from __future__ import annotations

import argparse
import importlib.util
import importlib.machinery
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cognilode-b4pt0r-chatmode"


def load_transport():
    loader = importlib.machinery.SourceFileLoader("b4pt0r_chatmode_transport", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code: int, *, content: bytes = b"", headers=None, chunks=None, stream_error=None):
        self.status_code = status_code
        self.content = content
        self.headers = dict(headers or {})
        self.text = content.decode("utf-8", "replace")
        self._chunks = list(chunks if chunks is not None else ([content] if content else []))
        self._stream_error = stream_error
        self.closed = False

    def iter_content(self, chunk_size=65536):
        del chunk_size
        for chunk in self._chunks:
            yield chunk
        if self._stream_error is not None:
            raise self._stream_error

    def close(self):
        self.closed = True


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.closed = False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        item = self.responses.pop(0)
        if callable(item):
            return item(kwargs)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def args_for(tmp_path: Path, *, conversation_id: str | None = "conv-1") -> argparse.Namespace:
    return argparse.Namespace(
        prompt="hello",
        prompt_file=None,
        conversation_id=conversation_id,
        parent_message_id="parent-1" if conversation_id else None,
        model="gpt-5-6-thinking",
        effort="high",
        output_dir=str(tmp_path / "out"),
        connector_id=[],
        attach=[],
        timeout=10,
        auth_source="codex",
        chrome_profile="/unused",
        impersonate="unused",
    )


def install_common(monkeypatch, mod, tmp_path: Path, session):
    monkeypatch.setattr(mod, "codex_identity", lambda: (session, {
        "access_token": "token",
        "account_id": "acct",
        "source": "codex_desktop_auth",
    }, "device"))
    monkeypatch.setattr(mod, "upload_attachments", lambda *a, **k: ([], 0))
    monkeypatch.setattr(mod, "prepare_payload", lambda session, auth, payload, device_id: ({"Accept": "text/event-stream"}, dict(payload)))
    monkeypatch.setattr(mod, "admit_central_conversation", lambda **kwargs: {"ok": True, "stored": True})
    monkeypatch.setattr(mod, "download_interpreter_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mod, "TASKFLOW_DIR", tmp_path / "taskflow")
    monkeypatch.setattr(mod, "MEMORY_QUEUE_DIR", tmp_path / "memory")


def sse_success(post_kwargs, *, conversation_id="conv-1", text="done"):
    user_id = post_kwargs["json"]["messages"][0]["id"]
    raw = (
        f'data: {{"conversation_id":"{conversation_id}","message_id":"{user_id}"}}\n\n'
        f'data: {{"message":{{"id":"assistant-1","author":{{"role":"assistant"}},'
        f'"content":{{"parts":["{text}"]}},"end_turn":true,"status":"finished_successfully"}}}}\n\n'
        'data: [DONE]\n\n'
    ).encode()
    return FakeResponse(200, content=raw, headers={"x-request-id": "req-ok"}, chunks=[raw])


def test_retry_after_distinguishes_numeric_and_http_date():
    mod = load_transport()
    assert mod._retry_after_seconds({"Retry-After": "7"}) == 7.0
    assert mod._retry_after_seconds({"retry-after": "99"}) == 99.0
    assert mod._retry_after_seconds({"retry-after": "9999"}) == mod.MAX_RETRY_AFTER_SECONDS
    assert mod._retry_after_seconds(
        {"Retry-After": "Thu, 01 Jan 1970 00:00:10 GMT"}, now=5.0
    ) == 5.0


def test_429_honors_retry_after_and_retries_only_explicit_rejection(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([
        FakeResponse(429, content=b"rate limited", headers={"Retry-After": "0"}),
        lambda kwargs: sse_success(kwargs),
    ])
    install_common(monkeypatch, mod, tmp_path, session)

    result = mod.send(args_for(tmp_path))

    assert result["ok"] is True
    assert result["state"] == "provider_accepted"
    assert result["recovery"]["mutation_attempts"] == 2
    assert result["recovery"]["rate_limit_retries"] == 1
    assert result["recovery"]["ambiguous_replay_suppressed"] is False
    assert len(session.posts) == 2
    assert Path(result["raw_path_host"]).exists()


@pytest.mark.parametrize("status", [502, 503])
def test_server_transient_never_replays_mutation_and_recovers_exact_turn(monkeypatch, tmp_path, status):
    mod = load_transport()
    session = SequenceSession([FakeResponse(status, content=b"bad gateway")])
    install_common(monkeypatch, mod, tmp_path, session)

    def reconcile(*args, **kwargs):
        return ({
            "accepted": True,
            "terminal": True,
            "state": "provider_accepted_recovered",
            "assistant_message_id": "assistant-2",
            "assistant_text": "recovered terminal",
            "events": [{"phase": "reconcile", "outcome": "terminal_recovered"}],
        }, session, args[2], args[3])

    monkeypatch.setattr(mod, "_reconcile_turn", reconcile)
    result = mod.send(args_for(tmp_path))

    assert len(session.posts) == 1
    assert result["ok"] is True
    assert result["state"] == "provider_accepted_recovered"
    assert result["terminal_assistant_text"] == "recovered terminal"
    assert result["recovery"]["mutation_attempts"] == 1
    assert result["recovery"]["ambiguous_replay_suppressed"] is True


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_refreshes_once_then_safe_retry(monkeypatch, tmp_path, status):
    mod = load_transport()
    first = SequenceSession([FakeResponse(status, content=b"expired")])
    second = SequenceSession([lambda kwargs: sse_success(kwargs)])
    install_common(monkeypatch, mod, tmp_path, first)
    refreshes = []

    def refresh(args, old_session):
        refreshes.append(old_session)
        return second, {"access_token": "fresh", "account_id": "acct", "source": "codex_desktop_auth"}, "device"

    monkeypatch.setattr(mod, "_refresh_auth_context", refresh)
    result = mod.send(args_for(tmp_path))

    assert len(first.posts) == 1
    assert len(second.posts) == 1
    assert len(refreshes) == 1
    assert result["ok"] is True
    assert result["recovery"]["authentication_refreshes"] == 1
    assert result["recovery"]["ambiguous_replay_suppressed"] is False


def test_404_missing_conversation_is_distinct_and_not_replayed(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([FakeResponse(404, content=b"missing")])
    install_common(monkeypatch, mod, tmp_path, session)
    monkeypatch.setattr(
        mod,
        "_hydrate_with_recovery",
        lambda *a, **k: (None, session, a[2], a[3], [{"phase": "hydrate", "outcome": "missing"}], "missing_conversation"),
    )

    result = mod.send(args_for(tmp_path))

    assert result["ok"] is False
    assert result["state"] == "missing_conversation"
    assert result["recovery"]["mutation_attempts"] == 1
    assert len(session.posts) == 1


def test_stream_break_after_2xx_never_replays_and_uses_readback(monkeypatch, tmp_path):
    mod = load_transport()

    def partial(post_kwargs):
        user_id = post_kwargs["json"]["messages"][0]["id"]
        chunk = f'data: {{"conversation_id":"conv-1","message_id":"{user_id}"}}\n\n'.encode()
        return FakeResponse(200, chunks=[chunk], stream_error=ConnectionError("websocket bridge closed"))

    session = SequenceSession([partial])
    install_common(monkeypatch, mod, tmp_path, session)

    def reconcile(*args, **kwargs):
        return ({
            "accepted": True,
            "terminal": True,
            "state": "provider_accepted_recovered",
            "assistant_message_id": "assistant-3",
            "assistant_text": "terminal after reconnect",
            "events": [],
        }, session, args[2], args[3])

    monkeypatch.setattr(mod, "_reconcile_turn", reconcile)
    result = mod.send(args_for(tmp_path))

    assert len(session.posts) == 1
    assert result["ok"] is True
    assert result["terminal_assistant_text"] == "terminal after reconnect"
    assert result["recovery"]["ambiguous_replay_suppressed"] is True
    assert any(event.get("classification") == "websocket_failure" for event in result["recovery"]["events"])
    attempt = Path(result["raw_path_host"])
    assert attempt.exists() and b"conversation_id" in attempt.read_bytes()


def test_provider_responses_failure_is_classified_as_ambiguous(monkeypatch, tmp_path):
    mod = load_transport()

    def failed(post_kwargs):
        user_id = post_kwargs["json"]["messages"][0]["id"]
        raw = (
            f'data: {{"conversation_id":"conv-1","message_id":"{user_id}"}}\n\n'
            'data: {"type":"response.failed","error":{"message":"responses backend failed"}}\n\n'
        ).encode()
        return FakeResponse(200, chunks=[raw])

    session = SequenceSession([failed])
    install_common(monkeypatch, mod, tmp_path, session)
    monkeypatch.setattr(mod, "_reconcile_turn", lambda *a, **k: ({
        "accepted": True,
        "terminal": False,
        "state": "provider_accepted_unfinished",
        "events": [],
    }, session, a[2], a[3]))

    result = mod.send(args_for(tmp_path))

    assert len(session.posts) == 1
    assert result["ok"] is True
    assert result["state"] == "provider_accepted_unfinished"
    assert result["recovery"]["ambiguous_replay_suppressed"] is True
    assert any(event.get("classification") == "responses_failure" for event in result["recovery"]["events"])


def test_terminal_artifact_is_written_to_durable_result_directory(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([lambda kwargs: sse_success(kwargs, text="file: sandbox:/mnt/data/report.txt")])
    install_common(monkeypatch, mod, tmp_path, session)

    def download(*args, **kwargs):
        target = kwargs["output_dir"] / "report.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("artifact body", encoding="utf-8")
        return [{"name": "report.txt", "path": str(target), "size": target.stat().st_size, "sha256": "test"}]

    monkeypatch.setattr(mod, "download_interpreter_artifacts", download)
    result = mod.send(args_for(tmp_path))

    assert result["ok"] is True
    assert len(result["downloaded_files"]) == 1
    artifact = Path(result["downloaded_files"][0]["path"])
    assert artifact.read_text(encoding="utf-8") == "artifact body"
    assert artifact.parent.name.endswith(".files")


def test_new_conversation_503_stays_ambiguous_and_never_replays(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([FakeResponse(503, content=b"service unavailable")])
    install_common(monkeypatch, mod, tmp_path, session)

    result = mod.send(args_for(tmp_path, conversation_id=None))

    assert result["ok"] is False
    assert result["state"] == "ambiguous_acceptance"
    assert len(session.posts) == 1
    assert result["recovery"]["ambiguous_replay_suppressed"] is True
    assert result["recovery"]["mutation_attempts"] == 1


def test_interpreter_downloads_are_atomic_and_collision_safe(tmp_path):
    mod = load_transport()

    class JsonResponse(FakeResponse):
        def __init__(self, payload):
            import json as _json
            self.payload = payload
            raw = _json.dumps(payload).encode()
            super().__init__(200, content=raw)
        def json(self):
            return self.payload

    class ArtifactSession:
        def __init__(self):
            self.calls = []
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if "interpreter/download?" in url:
                index = sum(1 for call, _ in self.calls if "interpreter/download?" in call)
                return JsonResponse({"download_url": f"https://download.invalid/{index}"})
            index = int(url.rsplit("/", 1)[-1])
            return FakeResponse(200, content=f"artifact-{index}".encode())

    session = ArtifactSession()
    downloads = mod.download_interpreter_artifacts(
        session,
        {"access_token": "t", "account_id": "a"},
        conversation_id="conv-1",
        assistant_message_id="assistant-1",
        assistant_text=(
            "first sandbox:/mnt/data/a/report.txt and "
            "second sandbox:/mnt/data/b/report.txt"
        ),
        output_dir=tmp_path,
        device_id="device",
    )

    assert [item["name"] for item in downloads] == ["report.txt", "report-2.txt"]
    assert [item["source_name"] for item in downloads] == ["report.txt", "report.txt"]
    paths = [Path(item["path"]) for item in downloads]
    assert [path.name for path in paths] == ["report.txt", "report-2.txt"]
    assert [path.read_bytes() for path in paths] == [b"artifact-1", b"artifact-2"]
    assert not list(tmp_path.glob("*.part"))


def test_prepare_503_retries_before_any_mutation(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([lambda kwargs: sse_success(kwargs)])
    install_common(monkeypatch, mod, tmp_path, session)
    calls = {"prepare": 0}

    def prepare(session_arg, auth, payload, device_id):
        calls["prepare"] += 1
        if calls["prepare"] == 1:
            raise mod.ProviderHTTPError("POST /backend-api/f/conversation/prepare", FakeResponse(503, content=b"prepare down"))
        return {"Accept": "text/event-stream"}, dict(payload)

    monkeypatch.setattr(mod, "prepare_payload", prepare)
    result = mod.send(args_for(tmp_path))

    assert result["ok"] is True
    assert calls["prepare"] == 2
    assert len(session.posts) == 1
    assert result["recovery"]["prepare_server_retries"] == 1
    assert result["recovery"]["mutation_attempts"] == 1

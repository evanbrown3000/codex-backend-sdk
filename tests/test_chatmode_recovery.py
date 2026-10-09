from __future__ import annotations

import argparse
import hashlib
import importlib.util
import importlib.machinery
import json
from pathlib import Path
import uuid

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


def test_queue_job_has_stable_user_id_and_existing_receipt_prevents_resend(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([lambda kwargs: sse_success(kwargs)])
    install_common(monkeypatch, mod, tmp_path, session)
    args = args_for(tmp_path)
    args.queue_job_id = "agent-memory:AM-7:chatgpt"
    monkeypatch.setenv("COGNILODE_CHATMODE_SEND_FENCE", __import__("json").dumps({
        "job_id": args.queue_job_id, "worker_id": "rhythm:evanpc", "lease_token": "fenced",
        "lease_generation": 1, "device_id": "evanpc"}))
    monkeypatch.setattr(mod, "operator_memory_post", lambda body: {"ok": True})

    result = mod.send(args)
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, "cognilode-chatmode-queue:" + args.queue_job_id))
    assert result["user_message_id"] == expected
    assert result["queue_job_id"] == args.queue_job_id
    with pytest.raises(mod.CapabilityError) as duplicate:
        mod.send(args)
    assert duplicate.value.code == "existing_queue_send_requires_reconciliation"
    assert len(session.posts) == 1


def test_physical_zip_upload_is_durable_and_d1_boundary_precedes_chat_post(monkeypatch, tmp_path):
    mod = load_transport()
    marked = []

    def post(kwargs):
        assert marked and marked[-1]["operation"] == "record_chatmode_post_started"
        return sse_success(kwargs)

    session = SequenceSession([post])
    install_common(monkeypatch, mod, tmp_path, session)
    zip_path = tmp_path / "research.zip"
    zip_path.write_bytes(b"PK\x03\x04physical zip payload")
    calls = []

    def upload(_session, _auth, path, *, device_id):
        calls.append(path)
        return {"id": "file_abc123", "name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size}, 3

    monkeypatch.setattr(mod, "upload_attachment", upload)
    monkeypatch.setattr(mod, "operator_memory_post", lambda body: marked.append(body) or {"ok": True})
    args = args_for(tmp_path)
    args.attach = [str(zip_path)]
    args.queue_job_id = "physical-zip-job"
    monkeypatch.setenv("COGNILODE_CHATMODE_SEND_FENCE", json.dumps({
        "job_id": args.queue_job_id, "worker_id": "rhythm:evanpc", "lease_token": "fenced",
        "lease_generation": 1, "device_id": "evanpc"}))

    result = mod.send(args)

    assert result["ok"] is True
    assert len(calls) == len(session.posts) == 1
    assert marked[0]["uploads"] == [{"id": "file_abc123", "name": "research.zip",
        "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(), "size": zip_path.stat().st_size}]
    assert len(marked[0]["receipt_sha256"]) == 64
    ledger = json.loads((Path(args.output_dir) / ("b4pt0r-chatmode-job-" +
        hashlib.sha256(args.queue_job_id.encode()).hexdigest()[:24] + ".uploads.json")).read_text())
    assert next(iter(ledger["files"].values()))["id"] == "file_abc123"
    ledger_path = Path(args.output_dir) / ("b4pt0r-chatmode-job-" +
        hashlib.sha256(args.queue_job_id.encode()).hexdigest()[:24] + ".uploads.json")
    reused, provider_requests, *_ = mod.upload_attachments_durable(
        args, session, {"access_token": "token", "account_id": "acct"}, "device",
        [str(zip_path)], ledger_path)
    assert reused[0]["id"] == "file_abc123"
    assert provider_requests == 0
    assert len(calls) == 1


def test_upload_retry_after_defers_without_chat_post_or_post_boundary(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([])
    install_common(monkeypatch, mod, tmp_path, session)
    zip_path = tmp_path / "research.zip"
    zip_path.write_bytes(b"PK\x03\x04physical zip payload")
    monkeypatch.setattr(mod, "upload_attachment", lambda *a, **k: (_ for _ in ()).throw(
        mod.ProviderHTTPError("upload", FakeResponse(502, content=b"gateway", headers={"Retry-After": "120"}))))
    marked = []
    monkeypatch.setattr(mod, "operator_memory_post", lambda body: marked.append(body) or {"ok": True})
    args = args_for(tmp_path)
    args.attach = [str(zip_path)]
    args.queue_job_id = "failed-upload-job"
    monkeypatch.setenv("COGNILODE_CHATMODE_SEND_FENCE", json.dumps({
        "job_id": args.queue_job_id, "worker_id": "rhythm:evanpc", "lease_token": "fenced",
        "lease_generation": 1, "device_id": "evanpc"}))

    with pytest.raises(mod.PrepostUploadError) as failure:
        mod.send(args)

    assert failure.value.http_status == 502
    assert failure.value.retry_after_seconds == 120
    assert session.posts == []
    assert marked == []


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

    def refresh(args, old_session, old_auth):
        refreshes.append(old_session)
        assert old_auth["account_id"] == "acct"
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
    assert result["recovery"]["ambiguous_replay_suppressed"] is True
    assert len(session.posts) == 1


def test_404_existing_conversation_does_not_authorize_duplicate_post(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([FakeResponse(404, content=b"gateway not found")])
    install_common(monkeypatch, mod, tmp_path, session)
    monkeypatch.setattr(mod, "_reconcile_turn", lambda *a, **k: ({
        "accepted": False, "terminal": False, "state": "ambiguous_acceptance", "events": [],
    }, session, a[2], a[3]))

    result = mod.send(args_for(tmp_path))

    assert len(session.posts) == 1
    assert result["state"] == "ambiguous_acceptance"
    assert result["recovery"]["ambiguous_replay_suppressed"] is True


def test_new_conversation_404_stays_ambiguous_for_exact_id_recent_read(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([FakeResponse(404, content=b"edge miss")])
    install_common(monkeypatch, mod, tmp_path, session)

    result = mod.send(args_for(tmp_path, conversation_id=None))

    assert len(session.posts) == 1
    assert result["state"] == "ambiguous_acceptance"
    assert result["recovery"]["ambiguous_replay_suppressed"] is True


def test_504_retry_after_defers_readback_without_duplicate_post(monkeypatch, tmp_path):
    mod = load_transport()
    session = SequenceSession([FakeResponse(504, content=b"gateway timeout", headers={"Retry-After": "120"})])
    install_common(monkeypatch, mod, tmp_path, session)
    monkeypatch.setattr(mod, "_reconcile_turn", lambda *a, **k: pytest.fail("read before Retry-After"))

    result = mod.send(args_for(tmp_path))

    assert len(session.posts) == 1
    assert result["state"] == "ambiguous_acceptance"
    assert result["retry_after_seconds"] == 120
    assert result["recovery"]["ambiguous_replay_suppressed"] is True


def test_codex_stale_token_refresh_uses_same_account_browser_session(monkeypatch):
    mod = load_transport()
    old = SequenceSession([])
    codex = SequenceSession([])
    browser = SequenceSession([])
    monkeypatch.setattr(mod, "codex_identity", lambda: (codex, {
        "access_token": "stale", "account_id": "acct", "source": "codex_desktop_auth"}, "codex-device"))
    monkeypatch.setattr(mod, "new_session", lambda *a: browser)
    monkeypatch.setattr(mod, "identity", lambda session: {
        "access_token": "fresh", "account_id": "acct", "source": "browser_auth_session"})
    monkeypatch.setattr(mod, "device_id_from_cookies", lambda session: "browser-device")
    args = argparse.Namespace(auth_source="codex", chrome_profile="/unused", impersonate="chrome")

    session, auth, device = mod._refresh_auth_context(args, old, {
        "access_token": "stale", "account_id": "acct"})

    assert old.closed and codex.closed
    assert session is browser
    assert auth["access_token"] == "fresh"
    assert device == "browser-device"


def test_health_uses_historical_browser_session_when_codex_token_gets_403(monkeypatch):
    mod = load_transport()
    codex = SequenceSession([])
    browser = SequenceSession([])
    monkeypatch.setattr(mod, "codex_identity", lambda: (codex, {
        "access_token": "stale", "account_id": "acct", "source": "codex_desktop_auth"}, "codex-device"))
    monkeypatch.setattr(mod, "new_session", lambda *a: browser)
    monkeypatch.setattr(mod, "identity", lambda session: {
        "access_token": "fresh", "account_id": "acct"})
    monkeypatch.setattr(mod, "device_id_from_cookies", lambda session: "browser-device")
    probes = []

    def sentinel(session, auth, *, device_id):
        probes.append((session, auth["access_token"], device_id))
        if session is codex:
            raise mod.ProviderHTTPError("sentinel", FakeResponse(403, content=b"expired"))
        return {}

    monkeypatch.setattr(mod, "sentinel_headers", sentinel)
    args = argparse.Namespace(auth_source="codex", chrome_profile="/unused", impersonate="chrome")
    value = mod.health(args)

    assert value["ok"] is True
    assert value["transport"] == "host_chrome_state_http"
    assert [(token, device) for _, token, device in probes] == [
        ("stale", "codex-device"), ("fresh", "browser-device")]


def test_refresh_never_crosses_provider_accounts(monkeypatch):
    mod = load_transport()
    old = SequenceSession([])
    codex = SequenceSession([])
    browser = SequenceSession([])
    monkeypatch.setattr(mod, "codex_identity", lambda: (codex, {
        "access_token": "stale", "account_id": "acct", "source": "codex_desktop_auth"}, "device"))
    monkeypatch.setattr(mod, "new_session", lambda *a: browser)
    monkeypatch.setattr(mod, "identity", lambda session: {
        "access_token": "other-token", "account_id": "other-acct"})
    args = argparse.Namespace(auth_source="codex", chrome_profile="/unused", impersonate="chrome")

    session, auth, _ = mod._refresh_auth_context(args, old, {
        "access_token": "stale", "account_id": "acct"})

    assert session is codex
    assert auth["account_id"] == "acct"
    assert browser.closed is True


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
            "provider_model_receipt": {"source": "provider_hydrated_terminal_message",
                                       "terminal_assistant_message_id": "assistant-3",
                                       "resolved_model_slug": "gpt-5-6-thinking",
                                       "thinking_effort": "xhigh",
                                       "hydrated_message_sha256": "a" * 64},
            "events": [],
        }, session, args[2], args[3])

    monkeypatch.setattr(mod, "_reconcile_turn", reconcile)
    result = mod.send(args_for(tmp_path))

    assert len(session.posts) == 1
    assert result["ok"] is True
    assert result["terminal_assistant_text"] == "terminal after reconnect"
    assert result["provider_model_receipt"]["thinking_effort"] == "xhigh"
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

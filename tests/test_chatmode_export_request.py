from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-chatmode-export-request"
loader = SourceFileLoader("chatmode_export_request_test", str(SCRIPT))
spec = spec_from_loader(loader.name, loader)
request = module_from_spec(spec)
loader.exec_module(request)


class FakeResponse:
    status_code = 202
    headers = {}

    def json(self):
        return {"status": "accepted"}


class FakeSession:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise TimeoutError("ambiguous timeout")
        return FakeResponse()


def snapshot(root):
    path = root / "privacy-export-backfill"
    path.mkdir()
    (path / "cursor.json").write_text(json.dumps({"archive_etag": '"etag"',
        "next_shard": 148, "deferred": []}))


def test_one_export_post_is_durable_and_waits_for_email(monkeypatch, tmp_path):
    snapshot(tmp_path)
    monkeypatch.setattr(request.sender, "identity", lambda _: {"account_id": "same-account",
        "user_email": "evanbrown.engineering@gmail.com"})
    monkeypatch.setattr(request.sender, "app_headers", lambda _: {"Authorization": "secret"})
    session = FakeSession()
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    first = request.run(tmp_path, now=now, session_factory=lambda: session)
    assert first["state"] == "accepted_waiting_export_email"
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/backend-api/accounts/data_export")
    ledger = json.loads((tmp_path / "privacy-export-requests.json").read_text())
    assert ledger["attempts"][-1]["state"] == "accepted_waiting_export_email"
    assert "secret" not in json.dumps(ledger)
    second = request.run(tmp_path, now=now + timedelta(days=1), session_factory=lambda: session)
    assert second["state"] == "awaiting_export_ready_or_retry_window"
    assert len(session.calls) == 1


def test_timeout_preserves_ambiguous_post_and_prevents_immediate_replay(monkeypatch, tmp_path):
    snapshot(tmp_path)
    monkeypatch.setattr(request.sender, "identity", lambda _: {"account_id": "same-account",
        "user_email": "evanbrown.engineering@gmail.com"})
    monkeypatch.setattr(request.sender, "app_headers", lambda _: {})
    session = FakeSession(fail=True)
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    first = request.run(tmp_path, now=now, session_factory=lambda: session)
    assert first["state"] == "post_effect_unknown_wait_for_export_email"
    second = request.run(tmp_path, now=now + timedelta(hours=1), session_factory=lambda: session)
    assert second["state"] == "awaiting_export_ready_or_retry_window"
    assert len(session.calls) == 1

from __future__ import annotations

import base64
import argparse
import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import uuid

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-b4pt0r-chatmode"


def transport():
    loader = SourceFileLoader("phone_chatmode_transport_test", str(SCRIPT))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def token(account: str, nonce: str) -> str:
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account}, "nonce": nonce}
    middle = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "header." + middle + ".signature"


def test_portable_provider_device_id_is_local_stable_and_private(monkeypatch, tmp_path):
    module = transport()
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("COGNILODE_CHATMODE_PORTABLE", "1")
    first = module.codex_device_id()
    assert str(uuid.UUID(first)) == first
    assert module.codex_device_id() == first
    path = tmp_path / ".config/cognilode/chatmode-provider-device-id"
    assert path.stat().st_mode & 0o777 == 0o600


def test_portable_refresh_preserves_account_and_atomically_replaces_token(monkeypatch, tmp_path):
    import codex_backend_sdk.oauth as oauth

    module = transport()
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    auth = tmp_path / ".codex/auth.json"
    auth.parent.mkdir()
    old = token("acct", "old")
    new = token("acct", "new")
    auth.write_text(json.dumps({"tokens": {"account_id": "acct", "access_token": old,
                                         "refresh_token": "refresh-old"}, "other": "preserved"}))
    monkeypatch.setattr(oauth, "refresh_access_token", lambda value: {
        "access_token": new, "refresh_token": "refresh-new"} if value == "refresh-old" else {})
    module._refresh_portable_codex_auth("acct", old)
    saved = json.loads(auth.read_text())
    assert saved["tokens"]["access_token"] == new
    assert saved["tokens"]["refresh_token"] == "refresh-new"
    assert saved["other"] == "preserved"
    assert auth.stat().st_mode & 0o777 == 0o600
    module._refresh_portable_codex_auth("acct", old)  # reread changed token; no second refresh


def test_portable_refresh_rejects_different_account_without_write(monkeypatch, tmp_path):
    import codex_backend_sdk.oauth as oauth

    module = transport()
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    auth = tmp_path / ".codex/auth.json"
    auth.parent.mkdir()
    old = token("acct", "old")
    original = json.dumps({"tokens": {"account_id": "acct", "access_token": old,
                                     "refresh_token": "refresh-old"}})
    auth.write_text(original)
    monkeypatch.setattr(oauth, "refresh_access_token", lambda _: {"access_token": token("other", "new")})
    with pytest.raises(module.CapabilityError) as raised:
        module._refresh_portable_codex_auth("acct", old)
    assert raised.value.code == "auth_account_mismatch"
    assert auth.read_text() == original


def test_recovery_uses_portable_refresh_only_after_rejected_stale_identity(monkeypatch):
    module = transport()
    monkeypatch.setenv("COGNILODE_CHATMODE_PORTABLE", "1")
    state = {"token": "stale", "refreshes": 0}
    sessions = []

    class Session:
        closed = False

        def close(self):
            self.closed = True

    def identity():
        session = Session()
        sessions.append(session)
        return session, {"access_token": state["token"], "account_id": "acct",
                         "source": "codex_portable_auth"}, "phone-device"

    def refresh(account, stale):
        assert (account, stale) == ("acct", "stale")
        state["token"] = "fresh"
        state["refreshes"] += 1

    monkeypatch.setattr(module, "codex_identity", identity)
    monkeypatch.setattr(module, "_refresh_portable_codex_auth", refresh)
    old = Session()
    args = argparse.Namespace(auth_source="codex", chrome_profile="/none", impersonate="chrome")
    current, auth, device = module._refresh_auth_context(args, old, {
        "access_token": "stale", "account_id": "acct"})
    assert state["refreshes"] == 1
    assert old.closed and sessions[0].closed
    assert current is sessions[1]
    assert auth["access_token"] == "fresh" and device == "phone-device"

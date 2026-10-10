from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-b4pt0r-collect"
loader = SourceFileLoader("b4pt0r_collector_test", str(SCRIPT))
spec = spec_from_loader(loader.name, loader)
collector = module_from_spec(spec)
loader.exec_module(collector)


def test_second_device_collector_uses_its_own_chrome_profile(monkeypatch, tmp_path):
    profile = tmp_path / "local-profile"
    profile.mkdir()
    monkeypatch.setenv("COGNILODE_CHATMODE_AUTH_SOURCE", "chrome")
    monkeypatch.setenv("COGNILODE_CHATMODE_CHROME_PROFILE", str(profile))
    seen = []
    class Session:
        def get(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {"mapping": {}})
    monkeypatch.setattr(collector.sender, "new_session", lambda p, _i: (seen.append(p), Session())[1])
    monkeypatch.setattr(collector.sender, "identity", lambda _s: {"access_token": "local"})
    monkeypatch.setattr(collector.sender, "device_id_from_cookies", lambda _s: "local-device")
    monkeypatch.setattr(collector.sender, "app_headers", lambda *_a, **_kw: {})
    _conversation, _session, _auth, device_id = collector.get_conversation("c-1", "unknown")
    assert seen == [profile]
    assert device_id == "local-device"


def test_exact_terminal_requires_final_assistant_after_exact_user():
    conversation = {
        "current_node": "final",
        "mapping": {
            "old": {"parent": None, "message": {"id": "old", "author": {"role": "user"}}},
            "target": {"parent": "old", "message": {"id": "target", "author": {"role": "user"}}},
            "progress": {"parent": "target", "message": {"id": "progress", "author": {"role": "assistant"}, "end_turn": False}},
            "final": {"parent": "progress", "message": {"id": "final", "author": {"role": "assistant"}, "end_turn": True}},
        },
    }
    user, assistant = collector.exact_terminal(conversation, "target")
    assert user["id"] == "target"
    assert assistant["id"] == "final"
    assert collector.exact_terminal(conversation, "missing") is None


def test_saved_terminal_tmp_zip_is_downloaded_without_conversation_poll(monkeypatch, tmp_path):
    stem = "b4pt0r-chatmode-tmp-link"
    receipt = tmp_path / f"{stem}.sse.receipt.json"
    receipt.write_text(json.dumps({"user_message_id": "user-id", "conversation_id": "conv-1"}))
    text = "[Download work](sandbox:/tmp/result.zip)"
    result = {"conversation_id": "conv-1", "assistant_terminal": True,
              "terminal_assistant_message_id": "assistant-1", "terminal_assistant_text": text,
              "downloaded_files": [], "central_conversation_store": {
                  "ok": True, "central_readback_verified": True,
                  "conversation_id": "conv-1", "central_tool_event_count": 0,
                  "central_raw_sse_source": {"readback_verified": True}}}
    record = {"prompt": "Produce a ZIP", "result": result}
    (tmp_path / f"{stem}.json").write_text(json.dumps(record))
    (tmp_path / f"{stem}.collected.json").write_text(json.dumps({**record, "complete": True}))
    assert collector.sender.sandbox_artifact_paths(text) == ["/tmp/result.zip"]
    fake_home = tmp_path / "provider-home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex/auth.json").write_text("{}")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(collector, "get_conversation", lambda *_args: pytest.fail("conversation poll"))
    runtime_calls = []
    monkeypatch.setattr(collector.sender, "ensure_runtime", lambda: runtime_calls.append(True))
    monkeypatch.setattr(collector.sender, "codex_identity", lambda: (object(), {}, "device"))
    def download(*_args, **kwargs):
        path = kwargs["output_dir"] / "result.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PK returned work")
        return [{"name": "result.zip", "sandbox_path": "/tmp/result.zip",
                 "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
    monkeypatch.setattr(collector.sender, "download_interpreter_artifacts", download)
    monkeypatch.setattr(collector.sender, "admit_central_conversation", lambda **_kw: {
        "ok": True, "central_readback_verified": True, "conversation_id": "conv-1",
        "central_artifacts": [{"sha256": "returned"}]})
    outcome = collector.collect(receipt, allow_provider_read=False)
    assert outcome["state"] == "collected_from_local_terminal"
    assert outcome["artifact_count"] == 1
    assert runtime_calls == [True]
    assert json.loads((tmp_path / f"{stem}.collected.json").read_text())["result"]["downloaded_files"][0]["sandbox_path"] == "/tmp/result.zip"


def test_saved_terminal_artifact_waits_for_credential_holder(monkeypatch, tmp_path):
    stem = "b4pt0r-chatmode-artifact-delegation"
    receipt = tmp_path / f"{stem}.sse.receipt.json"
    receipt.write_text(json.dumps({"user_message_id": "user-id", "conversation_id": "conv-1"}))
    record = {"prompt": "Produce a ZIP", "result": {
        "conversation_id": "conv-1", "assistant_terminal": True,
        "terminal_assistant_message_id": "assistant-1",
        "terminal_assistant_text": "[ZIP](sandbox:/tmp/result.zip)",
        "downloaded_files": [], "central_conversation_store": {"ok": True,
            "central_tool_event_count": 0,
            "central_raw_sse_source": {"readback_verified": True}}}}
    (tmp_path / f"{stem}.json").write_text(json.dumps(record))
    (tmp_path / f"{stem}.collected.json").write_text(json.dumps({**record, "complete": True}))
    monkeypatch.setenv("HOME", str(tmp_path / "without-auth"))
    monkeypatch.setattr(collector.sender, "ensure_runtime", lambda: pytest.fail("runtime installed in queue"))
    monkeypatch.setattr(collector.sender, "codex_identity", lambda: pytest.fail("provider credentials read in queue"))
    monkeypatch.setattr(collector, "get_conversation", lambda *_args: pytest.fail("conversation polled"))
    assert collector.collect(receipt, allow_provider_read=False)["state"] == "artifact_recovery_delegated"


def test_local_terminal_with_central_readback_needs_no_provider_read(tmp_path, monkeypatch):
    stem = "b4pt0r-chatmode-fixture"
    receipt_path = tmp_path / f"{stem}.sse.receipt.json"
    receipt_path.write_text(json.dumps({"user_message_id": "user-id", "state": "provider_accepted"}))
    (tmp_path / f"{stem}.json").write_text(json.dumps({
        "prompt": "Research the queue",
        "result": {
            "terminal_assistant_text": "Finished the work",
            "assistant_terminal": True,
            "central_conversation_store": {"ok": True, "central_readback_verified": True},
        },
    }))
    monkeypatch.setattr(collector, "get_conversation", lambda *args: pytest.fail("provider read was attempted"))
    result = collector.collect(receipt_path, allow_provider_read=False)
    assert result["state"] == "collected_from_local_terminal"
    assert json.loads((tmp_path / f"{stem}.collected.json").read_text())["complete"] is True


def test_completed_local_receipt_upgrades_tool_history_without_provider_read(tmp_path, monkeypatch):
    stem = 'b4pt0r-chatmode-upgrade'
    raw = tmp_path / f'{stem}.sse'
    raw.write_text('provider stream')
    receipt_path = tmp_path / f'{stem}.sse.receipt.json'
    receipt_path.write_text(json.dumps({'user_message_id':'user-id','raw_path_host':str(raw)}))
    collected = tmp_path / f'{stem}.collected.json'
    collected.write_text(json.dumps({'complete':True,'prompt':'Do the work','result':{
        'terminal_assistant_text':'Done','raw_sha256':'a'*64,
        'central_conversation_store':{'ok':True,'central_readback_verified':True}}}))
    monkeypatch.setattr(collector, 'get_conversation', lambda *args: pytest.fail('provider read was attempted'))
    observed = []
    def admit(**kwargs):
        observed.append(kwargs)
        return {'ok':True,'central_readback_verified':True,'central_tool_event_count':2,
                'central_raw_sse_source':{'uri':'s3://private/source.sse','readback_verified':True}}
    monkeypatch.setattr(collector.sender, 'admit_central_conversation', admit)
    result = collector.collect(receipt_path, allow_provider_read=False)
    assert result['state'] == 'central_tool_history_upgraded'
    assert observed[0]['result']['raw_path_host'] == str(raw)
    assert json.loads(collected.read_text())['result']['central_conversation_store']['central_tool_event_count'] == 2
    assert collector.collect(receipt_path, allow_provider_read=False)['state'] == 'already_collected'


def test_local_terminal_rejects_tampered_artifact(tmp_path):
    stem = "b4pt0r-chatmode-tampered"
    receipt_path = tmp_path / f"{stem}.sse.receipt.json"
    receipt_path.write_text(json.dumps({"user_message_id": "user-id", "state": "provider_accepted"}))
    artifact = tmp_path / "work.zip"
    artifact.write_bytes(b"tampered")
    (tmp_path / f"{stem}.json").write_text(json.dumps({
        "prompt": "Research the queue",
        "result": {
            "terminal_assistant_text": "[ZIP](sandbox:/mnt/data/work.zip)",
            "assistant_terminal": True,
            "downloaded_files": [{"path": str(artifact), "sha256": "0" * 64}],
            "central_conversation_store": {"ok": True, "central_readback_verified": True},
        },
    }))
    result = collector.collect(receipt_path, allow_provider_read=False)
    assert result["state"] == "awaiting_conversation_id"
    assert not (tmp_path / f"{stem}.collected.json").exists()


def test_retry_after_is_not_shortened():
    assert collector.retry_after_seconds("900") == 900
    assert collector.retry_after_seconds("55") >= 60


def test_terminal_without_zip_gets_fresh_read_and_preserves_first_terminal_time(monkeypatch, tmp_path):
    stem = "b4pt0r-chatmode-no-zip"
    receipt = tmp_path / f"{stem}.sse.receipt.json"
    receipt.write_text(json.dumps({"user_message_id": "user-id", "conversation_id": "conv-1",
                                   "state": "provider_accepted"}))
    collected = tmp_path / f"{stem}.collected.json"
    collected.write_text(json.dumps({"complete": True, "prompt": "Do work", "result": {
        "conversation_id": "conv-1", "assistant_terminal": True,
        "terminal_assistant_text": "No work ZIP yet", "completed_at": 1000,
        "downloaded_files": [], "central_conversation_store": {"ok": True,
            "central_readback_verified": True, "conversation_id": "conv-1",
            "central_tool_event_count": 0,
            "central_raw_sse_source": {"readback_verified": True}},
    }}))
    calls = []
    monkeypatch.setattr(collector, "get_conversation", lambda *args: (object(), object(), {}, "device"))
    monkeypatch.setattr(collector, "exact_terminal", lambda *args: ({"id": "user-id"}, {"id": "assistant-1"}))
    monkeypatch.setattr(collector.sender, "message_text", lambda msg: "Do work" if msg["id"] == "user-id" else "No work ZIP yet")
    zip_path = tmp_path / "work.zip"
    zip_path.write_bytes(b"PK fixture")
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    def downloads(*args, **kwargs):
        calls.append("GET")
        return [] if len(calls) == 1 else [{"name": "work.zip", "path": str(zip_path), "sha256": digest}]
    monkeypatch.setattr(collector.sender, "download_interpreter_artifacts", downloads)
    monkeypatch.setattr(collector.sender, "admit_central_conversation", lambda **kwargs: {
        "ok": True, "central_readback_verified": True, "conversation_id": "conv-1"})
    assert collector.collect(receipt)["state"] == "collected"
    assert json.loads(collected.read_text())["result"]["completed_at"] == 1000
    assert collector.collect(receipt)["state"] == "collected"
    assert len(calls) == 2
    assert json.loads(collected.read_text())["result"]["downloaded_files"][0]["sha256"] == digest

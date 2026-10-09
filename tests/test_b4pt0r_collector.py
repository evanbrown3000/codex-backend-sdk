from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-b4pt0r-collect"
loader = SourceFileLoader("b4pt0r_collector_test", str(SCRIPT))
spec = spec_from_loader(loader.name, loader)
collector = module_from_spec(spec)
loader.exec_module(collector)


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
        return {'ok':True,'central_readback_verified':True,'central_tool_event_count':2}
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

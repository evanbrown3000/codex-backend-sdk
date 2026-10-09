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


def test_retry_after_is_not_shortened():
    assert collector.retry_after_seconds("900") == 900
    assert collector.retry_after_seconds("55") >= 60

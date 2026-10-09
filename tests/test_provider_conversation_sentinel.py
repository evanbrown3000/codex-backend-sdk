from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cognilode-provider-conversation-sentinel"


def sentinel():
    loader = importlib.machinery.SourceFileLoader("provider_sentinel_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_stale_provider_does_not_dispatch_outside_single_queue():
    module = sentinel()
    module.TARGETS = [{"id": "chatgpt.com", "kind": "agent", "label": "ChatGPT.com"}]
    state = {"pending": [], "targets": {}}
    module.load_state = lambda: state
    module.save_state = lambda value: None
    module.reconcile_pending = lambda value: None
    module.list_target = lambda target: {"rows": [], "source": "operator_conversations"}
    module.freshness = lambda rows: {"fresh": False}
    module.materialize_listed_conversations = lambda *args: []
    module.emit = lambda *args: None
    module.publish_operator_panel_projection = lambda *args: None
    module.cli_call = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider dispatch attempted"))
    result = module.run_once()
    assert result["targets"]["chatgpt.com"]["remediation"] == {
        "dispatch_attempted": False, "reason": "direct_dispatch_retired_single_D1_queue"}


def test_pending_reconciliation_rotates_without_dropping_tail():
    module = sentinel()
    module.cli_call = lambda *args, **kwargs: {"rows": []}
    state = {"pending": [{"request_id": str(i), "agent_id": "a", "dispatch_receipt": {}}
                         for i in range(206)]}
    module.reconcile_pending(state)
    assert len(state["pending"]) == 206
    assert [row["request_id"] for row in state["pending"][:4]] == ["4", "5", "6", "7"]
    assert [row["request_id"] for row in state["pending"][-4:]] == ["0", "1", "2", "3"]

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts/cognilode-slack-role-collector"
loader = importlib.machinery.SourceFileLoader("slack_role_collector_test", str(PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
loader.exec_module(module)


def test_live_channel_inventory_and_resumable_role_history(tmp_path, monkeypatch):
    project = {"id": "C0C70QMKCEQ", "name": "proj-agent-memory", "is_member": True}
    team = {"id": "C0C70S09ZPE", "name": "team-provider-integrations", "is_member": False}
    unrelated = {"id": "C0C4MMWNC14", "name": "general", "is_member": True}
    observed = []

    def fake_call(cli, args):
        observed.append(args)
        if args[:2] == ["conversations", "list"]:
            return {"conversations": [project, team, unrelated], "next_cursor": None}
        cid = args[2]
        if cid == team["id"]:
            return {"channel_id": cid, "messages": [], "has_more": False}
        if "--latest" in args:
            return {"channel_id": cid, "messages": [
                {"ts": "1791232623.547069", "text": "Maya Chen — Staff Systems Integration Engineer — Platform Engineering"}],
                "has_more": False}
        if "--oldest" in args:
            return {"channel_id": cid, "messages": [], "has_more": False}
        return {"channel_id": cid, "messages": [
            {"ts": "1791518136.857879", "text": "ordinary project update"},
            {"ts": "1791518340.309089", "text": "Daniel Reyes — Staff Agent Runtime Engineer — Platform Engineering"}],
            "has_more": True}

    monkeypatch.setattr(module, "_call", fake_call)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    first = module.collect(Path("/unused/slackcli"), tmp_path)
    assert first["channels_discovered"] == 2
    assert first["channels_complete"] == 2
    assert first["channels_with_roles"] == 1
    snapshot = json.loads((tmp_path / "proj-agent-memory.json").read_text())
    assert snapshot["history_complete"] is True
    assert [row["ts"] for row in snapshot["messages"]] == ["1791232623.547069", "1791518340.309089"]

    observed.clear()
    second = module.collect(Path("/unused/slackcli"), tmp_path)
    assert second["channels_complete"] == 2
    assert not any("--latest" in call for call in observed)
    assert len(json.loads((tmp_path / "proj-agent-memory.json").read_text())["messages"]) == 2

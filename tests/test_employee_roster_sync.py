from __future__ import annotations

import importlib.machinery
import importlib.util
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts/cognilode-employee-roster-sync"
loader = importlib.machinery.SourceFileLoader("employee_roster_sync_test", str(PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
loader.exec_module(module)


def test_factual_candidate_discovery_rejects_status_lines(tmp_path):
    snapshot = tmp_path / "proj-work.json"
    snapshot.write_text(json.dumps({"channel_id": "C0C70QMKCEQ", "messages": [
        {"ts": "1791232623.547069", "text": "Maya Chen — Staff Systems Integration Engineer — Platform Engineering\nWork update."},
        {"ts": "1791412006.985459", "text": "Priya Nandakumar — Principal RPE Systems Engineer — 17:28 CT — execution note"},
        {"ts": "1791233225.884869", "text": "Daniel Reyes — Staff Agent Runtime Engineer — Platform Engineering"},
    ]}), encoding="utf-8")
    found = module.discover([tmp_path])
    assert sorted(found["candidates"]) == ["Daniel Reyes", "Maya Chen"]
    assert found["candidates"]["Maya Chen"]["slack_message"].endswith("p1791232623547069")
    assert module.role_from_message("Iris Vale — Collaboration Systems Engineer — durable handoff") is None
    assert module.role_from_message("Iris Vale — Collaboration Systems Engineer — Platform Engineering")


def test_activation_requires_named_completed_job_and_central_readback(tmp_path, monkeypatch):
    snapshots = tmp_path / "slack"
    snapshots.mkdir()
    (snapshots / "proj-work.json").write_text(json.dumps({"channel_id": "C0C70QMKCEQ", "messages": [
        {"ts": "1791232623.547069", "text": "Maya Chen — Staff Systems Integration Engineer — Platform Engineering"}
    ]}), encoding="utf-8")
    installed = tmp_path / "installed.json"
    installed.write_text(json.dumps({"schema": "cognilode.employee_slack_roles.v1", "employees": {}}), encoding="utf-8")
    candidates = tmp_path / "candidates.json"
    cid = "a1234567-1234-1234-1234-123456789abc"
    job = {"id": "tf-real", "state": "complete", "provider": "codex.research",
           "assigned_employee": "Maya Chen", "claimed_by": "Maya Chen",
           "prompt_author": "taskflow_plan", "taskflow_plan_receipt": {"plan_sha256": "a" * 64},
           "prompt": "Maya Chen — Staff Systems Integration Engineer — Platform Engineering",
           "effect_evidence": [{"kind": "research_zip", "ref": "b" * 64},
                               {"kind": "codex_session_identity", "job_id": "tf-real",
                                "conversation_id_source": "codex_cli_stderr_header",
                                "conversation_id": cid}]}
    executable = tmp_path / ".local/bin/codex"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)

    terminal = "Maya completed the research ZIP with a manifest."
    source_sha = "a" * 64
    readback = {"ok": True, "conversation": {"conversation_id": cid,
                "response_sha256": hashlib.sha256(terminal.encode()).hexdigest(),
                "capture": {"source_kind": "codex_rollout", "source_sha256": source_sha,
                            "source_response_complete": True, "source_complete": True,
                            "drive_verified": False},
                "events": [{"role": "assistant", "content": terminal}]}}

    def post(request):
        if request["operation"] == "list_jobs":
            return {"ok": True, "jobs": [job] if request["provider"] == "codex.research" else [], "next_cursor": None}
        assert request == {"operation": "read", "provider": "openai-codex", "conversation_id": cid}
        return readback

    readback["conversation"]["response_sha256"] = "0" * 64
    activations = tmp_path / "activations.json"
    result = module.reconcile([snapshots], installed_path=installed, candidates_path=candidates,
                              activations_path=activations, post=post)
    assert result["activated"] == []
    def lagging_post(request):
        if request["operation"] == "read":
            raise RuntimeError("central conversation not yet ingested")
        return post(request)
    result = module.reconcile([snapshots], installed_path=installed, candidates_path=candidates,
                              activations_path=activations, post=lagging_post)
    assert result["activated"] == []
    readback["conversation"]["response_sha256"] = hashlib.sha256(terminal.encode()).hexdigest()
    result = module.reconcile([snapshots], installed_path=installed, candidates_path=candidates,
                              activations_path=activations, post=post)
    assert result["activated"] == []
    readback["conversation"]["capture"].update({
        "drive_verified": True, "drive_source_sha256": source_sha,
        "drive_object_sha256": "b" * 64, "drive_verified_at": "2026-10-09T13:00:00Z",
        "drive_conversation_id": cid})
    result = module.reconcile([snapshots], installed_path=installed, candidates_path=candidates,
                              activations_path=activations, post=post)
    assert result["activated"] == ["Maya Chen"]
    assert json.loads(installed.read_text())["employees"]["Maya Chen"]["role"] == "Staff Systems Integration Engineer"
    assert json.loads(activations.read_text())["employees"]["Maya Chen"]["conversation_id"] == cid
    assert json.loads(activations.read_text())["employees"]["Maya Chen"]["drive_object_sha256"] == "b" * 64


def test_candidate_role_is_not_installed_role(tmp_path, monkeypatch):
    import importlib.machinery
    import importlib.util
    path = ROOT / "scripts/cognilode-taskflow-phase-controller"
    loader = importlib.machinery.SourceFileLoader("taskflow_candidate_role_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    controller = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = controller
    loader.exec_module(controller)
    installed = tmp_path / "installed.json"
    installed.write_text('{"employees":{}}')
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"candidates": {"Maya Chen": {
        "role": "Staff Systems Integration Engineer", "org": "Platform Engineering",
        "slack_message": "https://everything-ces8532.slack.com/archives/C0C70QMKCEQ/p1791232623547069"}}}))
    monkeypatch.setattr(controller, "SLACK_ROLES_PATH", installed)
    monkeypatch.setattr(controller, "SLACK_CANDIDATES_PATH", candidates)
    assert "Maya Chen — Staff Systems Integration Engineer" in controller.slack_role_context("Maya Chen")
    assert "Maya Chen" not in json.loads(installed.read_text())["employees"]

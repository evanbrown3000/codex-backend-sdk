from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Response:
    def __init__(self, status: int, value: dict, headers: dict | None = None):
        self.status_code = status
        self.value = value
        self.headers = headers or {}
        self.text = json.dumps(value)

    def json(self):
        return self.value


class GetOnlySession:
    def __init__(self, replies: list[Response]):
        self.replies = list(replies)
        self.urls: list[str] = []
        self.posts = 0

    def get(self, url, **kwargs):
        self.urls.append(url)
        assert kwargs.get("timeout", 0) <= 30
        return self.replies.pop(0)

    def post(self, *_args, **_kwargs):
        self.posts += 1
        raise AssertionError("ambiguous recovery must not replay POST")

    def close(self):
        pass


def branch(user_id: str, *, terminal: bool = True) -> dict:
    return {"current_node": "assistant", "mapping": {
        "user": {"parent": None, "message": {"id": user_id,
                 "author": {"role": "user"}, "content": {"parts": ["prompt"]}}},
        "assistant": {"parent": "user", "message": {"id": "assistant-1",
                      "author": {"role": "assistant"}, "recipient": "all",
                      "status": "finished_successfully", "end_turn": terminal,
                      "content": {"parts": ["terminal work report"]}}},
    }}


def args() -> argparse.Namespace:
    return argparse.Namespace(auth_source="codex", chrome_profile="/unused", impersonate="unused")


def test_recent_get_finds_exact_user_id_without_mutation():
    mod = load_script("cognilode-b4pt0r-chatmode")
    session = GetOnlySession([
        Response(200, {"items": [{"id": "other"}, {"id": "target"}]}),
        Response(200, branch("another-user")),
        Response(200, branch("stable-user")),
    ])
    found, _, _, _ = mod.discover_recent_turn(
        args(), session, {"access_token": "test", "account_id": "test"}, "device",
        user_message_id="stable-user", limit=12,
    )
    assert found["state"] == "provider_accepted_recovered"
    assert found["conversation_id"] == "target"
    assert found["assistant_text"] == "terminal work report"
    assert session.posts == 0
    assert "conversations?offset=0&limit=12&order=updated" in session.urls[0]


def test_recent_get_respects_retry_after_without_replay():
    mod = load_script("cognilode-b4pt0r-chatmode")
    session = GetOnlySession([Response(429, {"error": "slow down"}, {"Retry-After": "120"})])
    found, _, _, _ = mod.discover_recent_turn(
        args(), session, {"access_token": "test", "account_id": "test"}, "device",
        user_message_id="stable-user",
    )
    assert found["state"] == "rate_limited"
    assert found["retry_after_seconds"] == 120
    assert session.posts == 0


def test_intermediate_finished_successfully_is_not_terminal():
    mod = load_script("cognilode-b4pt0r-chatmode")
    session = GetOnlySession([Response(200, {"items": [{"id": "target"}]}),
                              Response(200, branch("stable-user", terminal=False))])
    found, _, _, _ = mod.discover_recent_turn(
        args(), session, {"access_token": "test", "account_id": "test"}, "device",
        user_message_id="stable-user",
    )
    assert found["accepted"] is True
    assert found["terminal"] is False
    assert found["state"] == "provider_accepted_unfinished"


def test_exact_submission_does_not_claim_later_users_terminal_reply():
    mod = load_script("cognilode-b4pt0r-chatmode")
    conversation = {"current_node": "assistant-2", "mapping": {
        "user-1": {"parent": None, "message": {"id": "user-1", "author": {"role": "user"}}},
        "user-2": {"parent": "user-1", "message": {"id": "user-2", "author": {"role": "user"}}},
        "assistant-2": {"parent": "user-2", "message": {"id": "assistant-2",
                        "author": {"role": "assistant"}, "end_turn": True,
                        "content": {"parts": ["reply to second user"]}}},
    }}
    try:
        mod.terminal_response_from_hydration(conversation, "user-1")
    except mod.CapabilityError as exc:
        assert exc.code == "provider_turn_unfinished"
    else:
        raise AssertionError("later assistant was assigned to the wrong user message")


def test_reconcile_writes_normal_result_and_central_readback(monkeypatch, tmp_path):
    mod = load_script("cognilode-b4pt0r-chatmode")
    monkeypatch.setattr(mod, "DEFAULT_OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(mod, "TASKFLOW_DIR", tmp_path / "taskflow")
    monkeypatch.setattr(mod, "MEMORY_QUEUE_DIR", tmp_path / "memory")
    session = GetOnlySession([])
    monkeypatch.setattr(mod, "codex_identity", lambda: (session, {"access_token": "test", "account_id": "test"}, "device"))
    monkeypatch.setattr(mod, "discover_recent_turn", lambda *a, **k: ({
        "state": "provider_accepted_recovered", "accepted": True, "terminal": True,
        "conversation_id": "conv-recovered", "assistant_message_id": "assistant-1",
        "assistant_text": "terminal work report", "events": []}, session, {}, "device"))
    work_zip = tmp_path / "work.zip"
    work_zip.write_bytes(b"mock zip bytes")
    monkeypatch.setattr(mod, "download_interpreter_artifacts", lambda *a, **k: [{
        "name": "work.zip", "path": str(work_zip), "sha256": hashlib.sha256(work_zip.read_bytes()).hexdigest()}])
    monkeypatch.setattr(mod, "admit_central_conversation", lambda **k: {
        "ok": True, "central_readback_verified": True, "conversation_id": "conv-recovered"})
    job_id = "ambiguous-job"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("original queued prompt")
    result = mod.reconcile_queue_job(argparse.Namespace(
        queue_job_id=job_id, prompt_file=str(prompt), output_dir="",
        auth_source="codex", chrome_profile="/unused", impersonate="unused",
        scan_offset=0, max_candidates=12,
    ))
    stem = "b4pt0r-chatmode-job-" + hashlib.sha256(job_id.encode()).hexdigest()[:24]
    record = json.loads((mod.DEFAULT_OUTPUT_ROOT / (stem + ".json")).read_text())
    assert result["assistant_terminal"] is True
    assert record["result"]["central_conversation_store"]["central_readback_verified"] is True
    assert record["result"]["user_message_id"] == str(uuid.uuid5(
        uuid.NAMESPACE_URL, "cognilode-chatmode-queue:" + job_id))
    assert (mod.MEMORY_QUEUE_DIR / (stem + ".json")).is_file()
    assert session.posts == 0


def test_known_only_reconcile_never_falls_back_to_recent_index(monkeypatch, tmp_path):
    mod = load_script("cognilode-b4pt0r-chatmode")
    monkeypatch.setattr(mod, "DEFAULT_OUTPUT_ROOT", tmp_path / "output")
    mod.DEFAULT_OUTPUT_ROOT.mkdir()
    job_id = "known-only-job"
    stem = "b4pt0r-chatmode-job-" + hashlib.sha256(job_id.encode()).hexdigest()[:24]
    (mod.DEFAULT_OUTPUT_ROOT / (stem + ".sse.receipt.json")).write_text(json.dumps({
        "queue_job_id": job_id, "conversation_id": "known-cid"}), encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("queued prompt", encoding="utf-8")
    session = GetOnlySession([])
    monkeypatch.setattr(mod, "codex_identity", lambda: (session, {}, "device"))
    calls = []
    def discover(*_args, **kwargs):
        calls.append(kwargs.get("known_conversation_id"))
        return ({"state": "not_found_in_recent", "accepted": False,
                 "retry_after_seconds": 300, "next_offset": 0, "events": []}, session, {}, "device")
    monkeypatch.setattr(mod, "discover_recent_turn", discover)
    result = mod.reconcile_queue_job(argparse.Namespace(
        queue_job_id=job_id, prompt_file=str(prompt), output_dir="",
        auth_source="codex", chrome_profile="/unused", impersonate="unused",
        scan_offset=0, max_candidates=12, known_only=True))
    assert result["state"] == "not_found_in_recent"
    assert calls == ["known-cid"]
    assert session.posts == 0


def test_worker_recovery_uses_only_reconcile_and_cools_down(monkeypatch, tmp_path):
    worker = load_script("cognilode-chatmode-queue-worker")
    monkeypatch.setattr(worker, "ROOT", tmp_path / "worker")
    calls = []
    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 2, json.dumps({
            "ok": False, "state": "rate_limited", "retry_after_seconds": 120,
            "next_offset": 12, "recovery": {"ambiguous_replay_suppressed": True},
        }), "")
    monkeypatch.setattr(worker.subprocess, "run", run)
    prompt = "exact queued prompt"
    job = {"id": "uncertain-job", "prompt": prompt,
           "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
    worker.reconcile_ambiguous(job)
    state = json.loads(worker.job_state_path(job["id"]).read_text())
    assert calls[0][2] == "reconcile"
    assert "send" not in calls[0]
    assert state["reconcile_scan_offset"] == 12
    assert 100 <= state["reconcile_next_at"] - __import__("time").time() <= 120
    assert state["reconcile_retry_after_seconds"] == 120
    worker.reconcile_ambiguous(job)
    assert len(calls) == 1


def test_worker_terminal_recovery_reaches_completion_without_send(monkeypatch, tmp_path):
    worker = load_script("cognilode-chatmode-queue-worker")
    monkeypatch.setattr(worker, "ROOT", tmp_path / "worker")
    observed = {"commands": [], "receipts": [], "completed": []}
    recovered = {"ok": True, "state": "provider_accepted_recovered",
                 "provider_acceptance_observed": True,
                 "conversation_id": "conv-recovered", "assistant_terminal": True,
                 "terminal_assistant_text": "real terminal report"}
    def run(command, **_kwargs):
        observed["commands"].append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(recovered), "")
    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker, "record_send_receipt",
                        lambda job, value, device: observed["receipts"].append((job["id"], value["conversation_id"])))
    monkeypatch.setattr(worker, "complete_from_result",
                        lambda job, value: observed["completed"].append((job["id"], value["conversation_id"])) or True)
    prompt = "exact queued prompt"
    job = {"id": "uncertain-job", "prompt": prompt,
           "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
    worker.reconcile_ambiguous(job)
    assert observed["commands"][0][2] == "reconcile"
    assert observed["receipts"] == [("uncertain-job", "conv-recovered")]
    assert observed["completed"] == [("uncertain-job", "conv-recovered")]


def test_recent_index_429_gates_other_jobs_but_allows_known_id_get(monkeypatch, tmp_path):
    worker = load_script("cognilode-chatmode-queue-worker")
    monkeypatch.setattr(worker, "ROOT", tmp_path / "worker")
    sender_root = tmp_path / "sender"
    sender_root.mkdir()
    monkeypatch.setattr(worker.sender, "DEFAULT_OUTPUT_ROOT", sender_root)
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            value = {"ok": False, "state": "rate_limited", "retry_after_seconds": 180,
                     "recovery": {"events": [{"phase": "recent_index", "http_status": 429}]}}
        else:
            value = {"ok": False, "state": "provider_accepted_unfinished",
                     "retry_after_seconds": 60}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(worker.subprocess, "run", run)
    def job(number):
        prompt = f"queued prompt {number}"
        return {"id": f"job-{number}", "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}

    worker.reconcile_ambiguous(job(1))
    gate = json.loads(worker.recent_index_gate_path().read_text())
    assert gate["retry_after_seconds"] == 180
    assert 160 <= gate["next_index_at"] - __import__("time").time() <= 180

    worker.reconcile_ambiguous(job(2))
    assert len(calls) == 1  # One 429 suppresses the next no-ID index scan.
    second = json.loads(worker.job_state_path("job-2").read_text())
    assert second["reconcile_provider_state"] == "recent_index_shared_cooldown"

    stem = worker.job_stem("job-3")
    (sender_root / (stem + ".sse.receipt.json")).write_text(json.dumps({
        "queue_job_id": "job-3", "conversation_id": "known-conversation"}), encoding="utf-8")
    worker.reconcile_ambiguous(job(3))
    assert len(calls) == 2
    assert "--known-only" in calls[1]

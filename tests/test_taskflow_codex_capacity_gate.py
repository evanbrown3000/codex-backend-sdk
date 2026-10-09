from datetime import UTC, datetime, timedelta
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts/cognilode-taskflow-phase-controller-service"
loader = importlib.machinery.SourceFileLoader("taskflow_capacity_service_test", str(PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
loader.exec_module(module)
NOW = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


def _status(path, remaining, at=NOW):
    path.write_text(json.dumps({"schema": "cognilode.modified_codex.llm_governance.v1",
        "observed_at": at.isoformat(), "quotas": [{"provider": "codex_backend",
        "observed_at": at.isoformat(), "remaining_percent": remaining}]}))


def _post(other=0, own_pending=False):
    jobs = [{"id": f"other-{n}", "state": "effect_pending", "project": "other-project",
             "taskflow_step": "X"} for n in range(other)]
    if own_pending:
        jobs.append({"id": "ours", "state": "effect_pending", "project": "employee-project-spine",
                     "taskflow_step": "MEMORY-1", "claimed_at": NOW.isoformat()})

    def post(request):
        return {"ok": True, "next_cursor": None,
                "jobs": jobs if request["provider"] == "codex.research"
                         and request["state"] == "effect_pending" else []}
    return post


def test_measured_quota_reduces_new_codex_concurrency_without_abandoning_own_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_project_id", lambda: "employee-project-spine")
    status = tmp_path / "status.json"
    _status(status, 61)
    assert module.capacity_gate(now=NOW, post=_post(other=1), status_path=status)["admit"] is True
    _status(status, 15)
    low = module.capacity_gate(now=NOW, post=_post(other=1), status_path=status)
    assert low["admit"] is False and low["max_active_codex"] == 1
    own = module.capacity_gate(now=NOW, post=_post(other=1, own_pending=True), status_path=status)
    assert own["admit"] is True and own["seed_step"] == "MEMORY-1"
    _status(status, 4)
    assert module.capacity_gate(now=NOW, post=_post(), status_path=status)["admit"] is False


def test_unknown_quota_allows_only_one_active_codex_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_project_id", lambda: "employee-project-spine")
    status = tmp_path / "status.json"
    _status(status, 99, at=NOW - timedelta(days=2))
    assert module.capacity_gate(now=NOW, post=_post(), status_path=status)["admit"] is True
    unknown = module.capacity_gate(now=NOW, post=_post(other=1), status_path=status)
    assert unknown["admit"] is False and unknown["quota_remaining_percent"] is None

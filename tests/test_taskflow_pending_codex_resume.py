from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from test_taskflow_phase_controller import FakeQueue, c, manifest_zip


CID = "12345678-1234-4234-9234-123456789abc"
HEADER = f"OpenAI Codex v0.162.0\n--------\nworkdir: /tmp\nsession id: {CID}\n--------\n"


def original_incomplete_run(run_dir: Path, binary: Path):
    source_run = run_dir / "original-secretary" / "attempt-01-installed-codex"
    source_run.mkdir(parents=True)
    (source_run / "codex.stderr").write_text(HEADER)
    (source_run / "delivery.txt").write_text(
        "Plan SHA-256: " + "a" * 64 + "\nStep: STEP-1 Effect\nWork ZIP SHA-256: " + "b" * 64)
    full = source_run / "receipt.json"
    full.write_text(json.dumps({"result": {"terminal": False}}))
    (run_dir / "secretary_receipt.json").write_text(json.dumps({
        "completed": False, "attempts": [{
            "candidate": {"family": "installed_codex", "metadata": {"binary": str(binary)}},
            "full_receipt_ref": str(full), "result": {"terminal": False},
        }],
    }))


def test_effect_pending_continues_exact_codex_session_and_checks_real_effect():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        binary = root / "codex"
        binary.write_text("binary identity")
        binary.chmod(0o700)
        original_incomplete_run(root, binary)
        plan = c.Plan("project", "Project", "a" * 64, "plan", (c.Step("STEP-1", "Effect", False, 0),))
        receipt = root / "secretary_receipt.json"
        calls = []

        def resume(argv, **kwargs):
            calls.append((argv, kwargs))
            Path(argv[argv.index("-o") + 1]).write_text("Applied and verified the real effect")
            return subprocess.CompletedProcess(argv, 0, "terminal", "provider stream")

        with mock.patch.object(c.subprocess, "run", side_effect=resume), \
             mock.patch.object(c, "verify_effect_receipt", return_value={"effect_ref": "deployed"}) as effect, \
             mock.patch.object(c, "verify_effect_probe", return_value={"observed": "real effect"}) as probe:
            result = c.resume_pending_codex_effect(
                pending={"id": "tf-" + "1" * 40}, plan=plan, step=plan.steps[0],
                run_dir=root, repo_path=root, receipt_path=receipt, work_sha256="b" * 64)
            repeated = c.resume_pending_codex_effect(
                pending={"id": "tf-" + "1" * 40}, plan=plan, step=plan.steps[0],
                run_dir=root, repo_path=root, receipt_path=receipt, work_sha256="b" * 64)
        assert result["state"] == "terminal_effect_verified"
        assert repeated["state"] == "terminal_receipt_already_present"
        assert len(calls) == 1
        assert calls[0][0][-2] == CID
        assert "do not repeat an external mutation" in calls[0][1]["input"]
        effect.assert_called_once()
        probe.assert_called_once()
        evidence = c.codex_session_identity_evidence(receipt, "tf-" + "1" * 40)
        assert evidence["conversation_id"] == CID


def test_missing_original_cli_header_cannot_create_a_new_codex_turn():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        binary = root / "codex"
        binary.write_text("binary identity")
        binary.chmod(0o700)
        original_incomplete_run(root, binary)
        (root / "original-secretary" / "attempt-01-installed-codex" / "codex.stderr").write_text(
            "user text: session id: " + CID)
        plan = c.Plan("project", "Project", "a" * 64, "plan", (c.Step("STEP-1", "Effect", False, 0),))
        with mock.patch.object(c.subprocess, "run") as sender:
            result = c.resume_pending_codex_effect(
                pending={"id": "tf-" + "1" * 40}, plan=plan, step=plan.steps[0],
                run_dir=root, repo_path=root, receipt_path=root / "secretary_receipt.json",
                work_sha256="b" * 64)
        assert result["state"] == "no_verified_original_codex_session"
        sender.assert_not_called()


def test_pending_external_effect_keeps_original_revision_after_plan_rollover():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plan_text = ("project Project\nid project\n[ ] STEP-1 Effect\n"
                     "    effect_probe_command: /usr/bin/printf observed\n"
                     "    effect_probe_expected: observed\n")
        original = c.parse_plan_text(plan_text)
        plan_file = root / "project.plan"
        plan_file.write_text(plan_text)
        original = c.replace(original, source_ref="file:" + str(plan_file))
        step = original.steps[0]
        q = FakeQueue()
        with mock.patch.object(c, "verify_plan_revision"):
            research = c.job_base(original, step, "research", provider="codex.research",
                                  priority=50, dependencies=[])
        research.update(state="complete", effect_evidence=[])
        q.jobs[research["id"]] = research
        work = root / "work.zip"
        manifest_zip(work, original.sha256, {
            "EXTERNAL_EFFECT_INSTRUCTIONS.md": b"apply", "patch.txt": b"changed"})
        meta = c.verify_manifest_zip(work, expected_plan_sha256=original.sha256,
                                     require_external_instructions=True)
        with mock.patch.object(c, "verify_plan_revision"):
            job = c.ensure_external_job(q, original, step, 50, meta,
                                        state_root=root / "state", external_employee="Rina Hale")
        claimed = q.claim("codex.external-effect", "Rina Hale", job["id"])
        assert claimed
        assert q.begin(claimed)["ok"]
        run_dir = root / "state" / "project" / "STEP-1" / "external_effect" / job["id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "EXTERNAL_EFFECT_RESULT.json").write_text(json.dumps({
            "schema": "cognilode.taskflow.external_effect.v1", "status": "applied",
            "project_id": "project", "plan_sha256": original.sha256,
            "step_id": "STEP-1", "work_zip_sha256": meta["sha256"],
            "effect_kind": "deployment", "effect_ref": "deploy:original-revision",
            "environment": "fixture", "checks": [{"command": "readback", "exit_code": 0,
                                               "result": "observed"}], "defects": [],
        }))
        (run_dir / "secretary_receipt.json").write_text(json.dumps({
            "taskflow_route_verified": True}))
        revised_text = plan_text.replace("[ ] STEP-1", "inflight_revision STEP-1 " + original.sha256 + "\n[ ] STEP-1")
        plan_file.write_text(revised_text)
        snapshot = c.plan_revision_path(plan_file, original.sha256)
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(plan_text)
        revised = c.replace(c.parse_plan_text(revised_text), source_ref="file:" + str(plan_file))
        assert c.step_execution_plan(revised, "STEP-1").sha256 == original.sha256
        with mock.patch.object(c, "verify_plan_revision"), \
             mock.patch.object(c, "execution_repo", return_value=root):
            result = c.run_once(queue=q, plan=revised, role="Elliot Mercer",
                                secretary=Path("/bin/false"), state_root=root / "state",
                                worker_id="unused", manager="project", priority=50,
                                external_employee="Rina Hale", seed_step="STEP-1")
        assert q.get(job["id"])["state"] == "complete"
        assert any(row.get("phase") == "external_effect_reconcile" and row.get("ok")
                   for row in result["actions"])

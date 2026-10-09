from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_taskflow_phase_controller import FakeQueue, c, manifest_zip


CID = "12345678-1234-4234-9234-123456789abc"
RUN_ID = "20261009T100000Z-abcdef0123"


def secretary_receipt(root: Path, stderr: str, *, conversation_id: str = CID,
                      reported_sha: str | None = None) -> dict:
    run_dir = root / "runs" / RUN_ID
    attempt_dir = run_dir / "attempt-01-codex"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "codex.stderr").write_text(stderr)
    digest = hashlib.sha256(stderr.encode()).hexdigest()
    result = {"conversation_id": conversation_id,
              "conversation_id_source": "codex_cli_stderr_header",
              "codex_stderr_sha256": reported_sha or digest}
    full = attempt_dir / "receipt.json"
    full.write_text(json.dumps({"result": result}))
    return {"run_id": RUN_ID, "run_dir": str(run_dir), "completed": True,
            "taskflow_route_verified": True,
            "attempts": [{"attempt": 1, "full_receipt_ref": str(full), "result": result}]}


def header(cid: str = CID) -> str:
    return f"OpenAI Codex v0.162.0\n--------\nworkdir: /tmp\nmodel: gpt-6-sol\nsession id: {cid}\n--------\nresponse body\n"


class EvidenceTests(unittest.TestCase):
    def test_bounded_diagnostic_before_real_header(self):
        diagnostic = "2026-10-09T13:06:22.112977Z ERROR rmcp::transport::worker: transport closed\n"
        self.assertEqual(c.codex_header_session_id(diagnostic + header()), CID)
        self.assertIsNone(c.codex_header_session_id("user\n" + header()))
        self.assertIsNone(c.codex_header_session_id(diagnostic * 17 + header()))

    def test_header_bound_receipt_and_old_receipt_omission(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = root / "secretary_receipt.json"
            receipt.write_text(json.dumps(secretary_receipt(root, header())))
            row = c.codex_session_identity_evidence(receipt, "tf-job-1")
            self.assertEqual(row["kind"], "codex_session_identity")
            self.assertEqual(row["conversation_id"], CID)
            self.assertEqual(row["conversation_id_source"], "codex_cli_stderr_header")
            self.assertEqual(row["job_id"], "tf-job-1")
            self.assertEqual(row["run_id"], RUN_ID)
            self.assertEqual(row["compact_receipt_sha256"], hashlib.sha256(receipt.read_bytes()).hexdigest())
            self.assertEqual(row["codex_stderr_sha256"], hashlib.sha256(header().encode()).hexdigest())
            self.assertIs(row["central_readback_verified"], False)
            old = root / "old_receipt.json"
            old.write_text(json.dumps({"taskflow_route_verified": True, "attempts": [{"result": {}}]}))
            self.assertIsNone(c.codex_session_identity_evidence(old, "old-job"))
            # New Codex stderr can be hashed while lacking a header session ID.
            no_header_id = root / "no_header_id.json"
            no_header_id.write_text(json.dumps({"attempts": [{"result": {
                "conversation_id": None, "conversation_id_source": None,
                "codex_stderr_sha256": "a" * 64}}]}))
            self.assertIsNone(c.codex_session_identity_evidence(no_header_id, "no-id-job"))

    def test_echoed_or_forged_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = root / "secretary_receipt.json"
            # The only matching ID occurs after the CLI's header fence.
            forged = f"OpenAI Codex v0.162.0\n--------\nworkdir: /tmp\n--------\nassistant echoed session id: {CID}\n"
            receipt.write_text(json.dumps(secretary_receipt(root, forged)))
            with self.assertRaisesRegex(ValueError, "header session ID"):
                c.codex_session_identity_evidence(receipt, "tf-job-1")
            receipt.write_text(json.dumps(secretary_receipt(root, header(), reported_sha="f" * 64)))
            with self.assertRaisesRegex(ValueError, "stderr SHA-256"):
                c.codex_session_identity_evidence(receipt, "tf-job-1")
            partial = secretary_receipt(root, header())
            partial["attempts"][0]["result"].pop("codex_stderr_sha256")
            receipt.write_text(json.dumps(partial))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                c.codex_session_identity_evidence(receipt, "tf-job-1")


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        path = self.repo / "plans" / "one.plan"
        path.parent.mkdir()
        path.write_text("project Identity\nid identity\n[ ] S applied effect\n"
                        "    effect_probe_command: /usr/bin/printf ok\n"
                        "    effect_probe_expected: ok\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "plans/one.plan"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "plan"], check=True)
        self.plan = c.parse_plan(path)
        self.step = self.plan.by_id()["S"]
        self.q = FakeQueue()
        self.state = self.repo / "state"

    def tick(self):
        return c.run_once(queue=self.q, plan=self.plan, role="Elliot Mercer", secretary=self.repo / "secretary",
                          state_root=self.state, worker_id="test", manager="manager", priority=50,
                          external_employee="Rina Hale", active_step_ids={"S"})

    def test_direct_research_completion_admits_exact_job_session_identity(self):
        def codex(_secretary, prompt_path, **_kwargs):
            out = prompt_path.parent / "research.zip"
            manifest_zip(out, self.plan.sha256, {"RESEARCH_REPORT.md": b"source evidence"})
            return secretary_receipt(self.repo / "secretary", header())
        with mock.patch.object(c, "run_secretary", side_effect=codex):
            self.tick()
        jid = c.phase_job_id(self.plan, "S", "research")
        job = self.q.get(jid)
        self.assertEqual(job["state"], "complete")
        proof = next(e for e in job["effect_evidence"] if e["kind"] == "codex_session_identity")
        self.assertEqual(proof["job_id"], jid)
        self.assertEqual(proof["conversation_id"], CID)
        self.assertTrue(c.codex_identity_readback(job, proof))

    def test_reconciled_research_completion_admits_identity_without_resend(self):
        research = c.materialize_research_job(self.q, self.plan, self.step, 50,
                                              role="Elliot Mercer", state_root=self.state)
        research.update(state="effect_pending", claimed_by="Elliot Mercer")
        run_dir = self.state / self.plan.project_id / "S" / "research" / research["id"]
        run_dir.mkdir(parents=True)
        manifest_zip(run_dir / "research.zip", self.plan.sha256, {"RESEARCH_REPORT.md": b"source evidence"})
        (run_dir / "secretary_receipt.json").write_text(json.dumps(secretary_receipt(self.repo / "secretary", header())))
        with mock.patch.object(c, "run_secretary", side_effect=AssertionError("duplicate Codex send")):
            self.tick()
        self.assertEqual(research["state"], "complete")
        self.assertEqual(next(e["ref"] for e in research["effect_evidence"] if e["kind"] == "codex_session_identity"), CID)

    def test_forged_research_session_stays_effect_pending(self):
        def codex(_secretary, prompt_path, **_kwargs):
            manifest_zip(prompt_path.parent / "research.zip", self.plan.sha256,
                         {"RESEARCH_REPORT.md": b"source evidence"})
            forged = f"OpenAI Codex v0.162.0\n--------\nworkdir: /tmp\n--------\nmodel echoed session id: {CID}\n"
            return secretary_receipt(self.repo / "secretary", forged)
        with mock.patch.object(c, "run_secretary", side_effect=codex):
            result = self.tick()
        job = self.q.get(c.phase_job_id(self.plan, "S", "research"))
        self.assertEqual(job["state"], "effect_pending")
        self.assertEqual(job["effect_evidence"], [])
        self.assertTrue(any(a.get("phase") == "research_effect" and a.get("ok") is False
                            for a in result["actions"]))

    def external_setup(self):
        research = c.job_base(self.plan, self.step, "research", provider="codex.research",
                              priority=50, dependencies=[])
        research.update(state="complete", effect_evidence=[{"kind": "research_zip", "ref": "a" * 64}])
        self.q.jobs[research["id"]] = research
        work = self.repo / "work.zip"
        manifest_zip(work, self.plan.sha256,
                     {"EXTERNAL_EFFECT_INSTRUCTIONS.md": b"apply and verify", "src/a.py": b"print(1)"})
        meta = c.verify_manifest_zip(work, expected_plan_sha256=self.plan.sha256,
                                     require_external_instructions=True)
        job = c.ensure_external_job(self.q, self.plan, self.step, 50, meta,
                                    state_root=self.state, external_employee="Rina Hale")
        receipt = (self.state / self.plan.project_id / "S" / "external_effect" /
                   job["id"] / "EXTERNAL_EFFECT_RESULT.json")
        return job, receipt, meta

    def write_external_receipt(self, path: Path, meta: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": "cognilode.taskflow.external_effect.v1",
                                    "status": "applied", "project_id": self.plan.project_id,
                                    "plan_sha256": self.plan.sha256, "step_id": "S",
                                    "work_zip_sha256": meta["sha256"], "effect_kind": "deployment",
                                    "effect_ref": "deploy:fixture", "environment": "fixture",
                                    "checks": [{"command": "readback", "exit_code": 0,
                                                "result": "deployed object exists"}], "defects": []}))

    def test_direct_external_completion_admits_exact_job_session_identity(self):
        job, receipt, meta = self.external_setup()
        def codex(_secretary, _prompt_path, **_kwargs):
            self.write_external_receipt(receipt, meta)
            return secretary_receipt(self.repo / "secretary", header())
        with mock.patch.object(c, "run_secretary", side_effect=codex):
            result = self.tick()
        self.assertEqual(job["state"], "complete")
        proof = next(e for e in job["effect_evidence"] if e["kind"] == "codex_session_identity")
        self.assertEqual(proof["job_id"], job["id"])
        self.assertEqual(proof["conversation_id"], CID)
        self.assertTrue(any(a.get("phase") == "external_effect_complete" and a.get("ok")
                            for a in result["actions"]))

    def test_reconciled_external_completion_admits_identity_without_resend(self):
        job, receipt, meta = self.external_setup()
        claimed = self.q.claim("codex.external-effect", "Rina Hale", job["id"])
        self.assertIsNotNone(claimed)
        self.assertTrue(self.q.begin(claimed)["ok"])
        self.write_external_receipt(receipt, meta)
        receipt.with_name("secretary_receipt.json").write_text(
            json.dumps(secretary_receipt(self.repo / "secretary", header())))
        with mock.patch.object(c, "run_secretary", side_effect=AssertionError("duplicate Codex send")):
            result = self.tick()
        self.assertEqual(job["state"], "complete")
        self.assertEqual(next(e["ref"] for e in job["effect_evidence"] if e["kind"] == "codex_session_identity"), CID)
        self.assertTrue(any(a.get("phase") == "external_effect_reconcile" and a.get("ok")
                            for a in result["actions"]))


if __name__ == "__main__":
    unittest.main()

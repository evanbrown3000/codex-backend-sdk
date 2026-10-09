from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_taskflow_phase_controller import FakeQueue, c, manifest_zip


class NativeRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        plan_path = self.repo / "plans" / "one.plan"
        plan_path.parent.mkdir()
        plan_path.write_text("project Native Repair\nid native-repair\n[ ] S real deployed effect\n"
                             "    effect_probe_command: /usr/bin/printf ok\n"
                             "    effect_probe_expected: ok\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "plans/one.plan"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "plan"], check=True)
        self.plan = c.parse_plan(plan_path)
        self.step = self.plan.by_id()["S"]
        self.state = self.repo / "state"
        self.q = FakeQueue()
        research_zip = self.repo / "research.zip"
        manifest_zip(research_zip, self.plan.sha256, {"RESEARCH_REPORT.md": b"source evidence"})
        self.research_digest = hashlib.sha256(research_zip.read_bytes()).hexdigest()
        research = c.job_base(self.plan, self.step, "research", provider="codex.research", priority=50, dependencies=[])
        research.update(state="complete", effect_evidence=[{"kind": "research_zip", "ref": self.research_digest,
                                                             "path": str(research_zip)}])
        self.q.jobs[research["id"]] = research
        self.work_zip = self.repo / "work.zip"
        manifest_zip(self.work_zip, self.plan.sha256,
                     {"EXTERNAL_EFFECT_INSTRUCTIONS.md": b"deploy work", "src/a.py": b"print(1)"})
        self.work_digest = hashlib.sha256(self.work_zip.read_bytes()).hexdigest()
        prompt = c.chatgpt_prompt(self.plan, self.step, {"sha256": self.research_digest})
        chat = c.job_base(self.plan, self.step, "chatgpt_sandbox", provider="chatgpt.com", priority=50, dependencies=[])
        chat.update(state="complete", prompt=prompt, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    assigned_employee="ChatGPT chat-mode native sandbox", reasoning_effort="xhigh",
                    attachment_refs=[{"ref": "file:" + str(research_zip), "sha256": self.research_digest}])
        self.q.jobs[chat["id"]] = chat
        self.original = chat

    def complete_chat(self, job, *, native=False):
        cid = "conversation-" + job["id"]
        evidence = [{"kind": "chatgpt_sandbox_artifact", "ref": self.work_digest, "path": str(self.work_zip)},
                    {"kind": "provider_conversation", "ref": cid},
                    {"kind": "central_conversation_readback", "ref": cid}]
        if native:
            evidence.append({"kind": "provider_observed_native_exec", "tool": "container.exec",
                             "ref": "result-1", "call_ref": "call-1", "raw_stream_sha256": "a" * 64})
        job.update(state="complete", conversation_id=cid, effect_evidence=evidence)

    def complete_without_zip(self, job):
        cid = "conversation-" + job["id"]
        job.update(state="complete", conversation_id=cid, effect_evidence=[
            {"kind": "provider_conversation", "ref": cid},
            {"kind": "central_conversation_readback", "ref": cid},
            {"kind": "chatgpt_terminal_deliverable_failure", "ref": "b" * 64,
             "conversation_id": cid, "terminal_assistant_message_id": "terminal-1",
             "reason": "no_usable_single_work_zip_after_bounded_get_collection"}])

    def tick(self):
        with mock.patch.object(c, "run_secretary", return_value={"completed": False}):
            return c.run_once(queue=self.q, plan=self.plan, role="Elliot Mercer", secretary=self.repo / "secretary",
                              state_root=self.state, worker_id="test-worker", manager="Manager", priority=50,
                              external_employee="Rina Hale", active_step_ids={"S"})

    def test_missing_native_proof_queues_one_retry_without_effect_handoff(self):
        self.complete_chat(self.original)
        first = self.tick()
        retry_id = c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 1)
        retry = self.q.get(retry_id)
        self.assertEqual(retry["state"], "queued")
        self.assertEqual(retry["attachment_refs"], self.original["attachment_refs"])
        self.assertEqual(retry["taskflow_dependencies"], [self.original["id"]])
        self.assertEqual(retry["phase_attempt"], 1)
        self.assertIn(self.original["prompt"], retry["prompt"])
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "external_effect")))
        self.assertTrue(any(a.get("phase") == "native_compute_repair" for a in first["actions"]))
        self.tick()
        self.assertEqual(sum(1 for j in self.q.jobs.values() if j.get("provider") == "chatgpt.com"), 2)

    def test_d1_row_without_local_only_plan_or_attempt_fields_recovers(self):
        self.complete_chat(self.original)
        self.original["taskflow_plan_receipt"] = {"plan_sha256": self.plan.sha256,
                                                  "taskflow_step": "S", "phase": "chatgpt_sandbox"}
        for key in ("plan_text", "plan_source_ref", "payload", "phase_attempt"):
            self.original.pop(key, None)
        self.tick()
        retry = self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 1))
        self.assertIsNotNone(retry)
        for key in ("plan_text", "plan_source_ref", "payload", "phase_attempt"):
            retry.pop(key, None)
        self.complete_chat(retry, native=True)
        result = self.tick()
        self.assertTrue(any(a.get("phase") == "external_effect" for a in result["actions"]))

    def test_ambiguous_inflight_turn_is_never_retried(self):
        self.original["state"] = "claimed"
        self.tick()
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 1)))
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "external_effect")))

    def test_definitive_missing_zip_gets_bounded_rhythm_retry_and_diagnostic(self):
        self.complete_without_zip(self.original)
        first = self.tick()
        retry1 = self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 1))
        self.assertEqual(retry1["state"], "queued")
        self.assertEqual(retry1["attachment_refs"], self.original["attachment_refs"])
        self.assertEqual(retry1["reason"], "chatmode_deliverable_repair_attempt:1")
        self.assertIn("did not provide a usable work ZIP", retry1["prompt"])
        self.assertTrue(any(a.get("phase") == "chatgpt_deliverable_repair" for a in first["actions"]))
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "external_effect")))
        self.complete_without_zip(retry1)
        self.tick()
        retry2 = self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 2))
        self.complete_without_zip(retry2)
        result = self.tick()
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 3)))
        diagnostic = self.q.get(c.native_diagnostic_job_id(self.plan, self.step))
        self.assertEqual(diagnostic["provider"], "codex.research")
        self.assertIn("missing work ZIPs", diagnostic["prompt"])
        failure = json.loads((self.state / self.plan.project_id / "S" / "native_compute_failure.json").read_text())
        self.assertEqual(failure["defect_kinds"], ["missing_work_zip"] * 3)
        self.assertTrue(any(a.get("phase") == "chatgpt_phase_exhausted" for a in result["actions"]))
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "external_effect")))

    def test_existing_inflight_external_effect_suppresses_old_evidence_retry(self):
        self.complete_chat(self.original)
        effect = c.job_base(self.plan, self.step, "external_effect", provider="codex.external-effect",
                            priority=50, dependencies=[])
        effect.update(state="effect_pending", claimed_by="Rina Hale")
        self.q.jobs[effect["id"]] = effect
        result = self.tick()
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 1)))
        self.assertTrue(any(a.get("phase") == "native_repair_superseded_by_external"
                            and a.get("external_job_id") == effect["id"] for a in result["actions"]))

    def test_existing_retry_with_changed_prompt_is_rejected(self):
        self.complete_chat(self.original)
        self.tick()
        retry = self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 1))
        retry["prompt"] = "different instruction"
        result = self.tick()
        self.assertTrue(any(a.get("phase") == "native_compute_repair" and a.get("ok") is False
                            for a in result["actions"]))
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "external_effect")))

    def test_successful_repair_hands_off_exact_returned_zip(self):
        self.complete_chat(self.original)
        self.tick()
        retry = self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 1))
        self.complete_chat(retry, native=True)
        result = self.tick()
        effect = self.q.get(c.phase_job_id(self.plan, "S", "external_effect"))
        self.assertEqual(effect["state"], "effect_pending")
        self.assertEqual(effect["attachment_refs"][0]["sha256"], self.work_digest)
        self.assertTrue(any(a.get("phase") == "external_effect" and a.get("chat_attempt") == 1
                            for a in result["actions"]))

    def test_three_definitive_failures_route_named_codex_diagnostic_without_more_sends(self):
        self.complete_chat(self.original)
        self.tick()
        for attempt in (1, 2):
            retry = self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", attempt))
            self.complete_chat(retry)
            result = self.tick()
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", 3)))
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "external_effect")))
        failure = json.loads((self.state / self.plan.project_id / "S" / "native_compute_failure.json").read_text())
        self.assertEqual(failure["state"], "diagnostic_routed")
        self.assertEqual(len(failure["attempt_job_ids"]), 3)
        diagnostic = self.q.get(c.native_diagnostic_job_id(self.plan, self.step))
        self.assertEqual(diagnostic["provider"], "codex.research")
        self.assertEqual(diagnostic["assigned_employee"], "Elliot Mercer")
        self.assertEqual(diagnostic["state"], "effect_pending")
        self.assertEqual(diagnostic["taskflow_dependencies"], failure["attempt_job_ids"])
        self.assertEqual(failure["diagnostic_job_id"], diagnostic["id"])
        self.assertEqual(failure["shared_store"], "d1_job_queue")
        self.assertTrue(any(a.get("phase") == "native_compute_exhausted" for a in result["actions"]))
        self.tick()
        self.assertEqual(sum(1 for j in self.q.jobs.values() if j.get("provider") == "chatgpt.com"), 3)
        self.assertEqual(sum(1 for j in self.q.jobs.values() if j.get("reason") == "native_compute_diagnostic"), 1)

    def test_named_codex_diagnostic_report_is_completed_and_read_from_shared_d1(self):
        self.complete_chat(self.original)
        self.tick()
        for attempt in (1, 2):
            retry = self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", attempt))
            self.complete_chat(retry)
            self.tick()
        jid = c.native_diagnostic_job_id(self.plan, self.step)
        diagnostic = self.q.get(jid)
        self.assertEqual(diagnostic["state"], "effect_pending")
        report_path = (self.state / self.plan.project_id / "S" / "native_compute_diagnostic" /
                       jid / "NATIVE_COMPUTE_DIAGNOSTIC.json")
        sources = [{"job_id": self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", n))["id"],
                    "conversation_id": self.q.get(c.phase_job_id(self.plan, "S", "chatgpt_sandbox", n))["conversation_id"],
                    "finding": "No provider-native tool pair was captured"} for n in range(3)]
        report_path.write_text(json.dumps({"schema": "cognilode.taskflow.native_compute_diagnostic.v1",
                                           "sources": sources, "recommended_fix": "Inspect exact provider SSE parser",
                                           "revised_research_notes": "Compare raw streams with D1 evidence"}))
        (report_path.parent / "secretary_receipt.json").write_text(json.dumps({"taskflow_route_verified": True}))
        result = self.tick()
        self.assertEqual(diagnostic["state"], "complete")
        proof = next(e for e in diagnostic["effect_evidence"] if e["kind"] == "native_diagnostic_report")
        self.assertEqual(hashlib.sha256(proof["report_text"].encode()).hexdigest(), proof["ref"])
        self.assertEqual(proof["sources"], [{"job_id": r["job_id"], "conversation_id": r["conversation_id"]}
                                            for r in sources])
        self.assertTrue(any(a.get("phase") == "native_compute_diagnostic" and a.get("state") == "complete"
                            for a in result["actions"]))
        self.assertIsNone(self.q.get(c.phase_job_id(self.plan, "S", "external_effect")))
        # The shared D1 evidence remains readable after this device loses its files.
        report_path.unlink()
        (self.state / self.plan.project_id / "S" / "native_compute_failure.json").unlink()
        recovered = self.tick()
        self.assertTrue(any(a.get("phase") == "native_compute_diagnostic" and a.get("state") == "complete"
                            and a.get("report_sha256") == proof["ref"] for a in recovered["actions"]))


if __name__ == "__main__":
    unittest.main()

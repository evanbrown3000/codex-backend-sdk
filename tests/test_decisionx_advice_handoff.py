from __future__ import annotations

from hashlib import sha256
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-decisionx-advice-handoff"
loader = importlib.machinery.SourceFileLoader("decisionx_advice_handoff_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
handoff = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = handoff
loader.exec_module(handoff)


def write_zip(path: Path, members: dict[str, bytes]) -> str:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return sha256(path.read_bytes()).hexdigest()


class AdviceHandoffTests(unittest.TestCase):
    def test_uploaded_candidate_packet_matches_shared_sources_and_segments(self):
        at = "2024-01-01T00:00:00+00:00"
        source = {"provider": "openai-codex", "conversation_id": "old-1",
                  "prompt_sha256": "a" * 64, "response_sha256": "b" * 64,
                  "capture": {"drive_verified": True, "source_complete": True,
                              "source_response_complete": True, "source_sha256": "c" * 64,
                              "drive_source_sha256": "c" * 64, "drive_object_sha256": "d" * 64,
                              "drive_verified_at": at, "drive_conversation_id": "old-1"},
                  "events": [{"role": "user", "content": "Historic human intent", "created_at": at},
                             {"role": "assistant", "content": "Historic agent action", "created_at": at}]}
        ref = {"provider": "openai-codex", "conversation_id": "old-1",
               "prompt_sha256": "a" * 64, "response_sha256": "b" * 64,
               "source_sha256": "c" * 64, "source_at_utc": at}
        candidate = {"segment_id": "decisionx.iae.abc", **{k: ref[k] for k in
                     ("provider", "conversation_id", "prompt_sha256", "response_sha256", "source_sha256")}}
        segment = {"segment_id": "decisionx.iae.abc", "kind": "IAE",
                   "source_key": "openai-codex:old-1",
                   "metadata": {"drive_verified_source": True,
                                "full_source_prompt_sha256": "a" * 64,
                                "full_source_response_sha256": "b" * 64}}
        target = {"provider": "chatgpt.com", "conversation_id": "current-1",
                  "prompt_sha256": "e" * 64, "response_sha256": "f" * 64,
                  "events": [{"role": "user", "content": "Choose a next action"},
                             {"role": "assistant", "content": "The last action completed"}]}
        def post(body):
            if body["operation"] == "read":
                if body["provider"] == "chatgpt.com":
                    return {"ok": True, "conversation": target}
                return {"ok": True, "conversation": source}
            if body["operation"] == "read_segment":
                return {"ok": True, "segment": segment}
            if body["operation"] == "get_job":
                return {"ok": True, "job": {"id": "target-1", "state": "complete",
                                            "provider": "chatgpt.com", "conversation_id": "current-1"}}
            raise AssertionError(body)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "candidates.zip"
            target_raw = handoff.canonical(target)
            candidates_raw = handoff.canonical([candidate])
            first_sha = write_zip(first, {
                "MANIFEST.json": handoff.canonical(
                    {"schema": "cognilode.decisionx.successor_input.v1",
                     "target_job_id": "target-1", "target_provider": "chatgpt.com",
                     "target_conversation_id": "current-1",
                     "target_prompt_sha256": "e" * 64,
                     "target_response_sha256": "f" * 64,
                     "source_refs": [ref], "candidate_refs": [candidate],
                     "source_refs_sha256": handoff.digest(handoff.canonical([ref])),
                     "target_json_sha256": sha256(target_raw).hexdigest(),
                     "candidates_json_sha256": sha256(candidates_raw).hexdigest()}),
                "target.json": target_raw, "candidates.json": candidates_raw})
            full = Path(directory) / "sources.zip"
            source_raw = handoff.canonical(source)
            full_sha = write_zip(full, {
                "MANIFEST.json": handoff.canonical({"schema": "cognilode.root_memory_sources.v1",
                   "parts": [{"path": "parts/old-1.0", "provider": "openai-codex",
                              "conversation_id": "old-1", "part_index": 0, "part_count": 1,
                              "source_json_sha256": sha256(source_raw).hexdigest(),
                              "part_sha256": sha256(source_raw).hexdigest()}]}),
                "parts/old-1.0": source_raw})
            job = {"attachment_refs": [{"ref": str(first), "sha256": first_sha},
                                       {"ref": str(full), "sha256": full_sha}]}
            central = {"provider_structured_uploads": [{"sha256": first_sha}, {"sha256": full_sha}]}
            hashes, sources, candidates, source_sha, historical_sha = handoff.verify_inputs(post, job, central)
            self.assertEqual(hashes, [first_sha, full_sha])
            self.assertEqual(sources[0]["conversation_id"], "old-1")
            self.assertEqual(candidates[0]["segment_id"], "decisionx.iae.abc")
            self.assertEqual(source_sha, handoff.digest(handoff.canonical([ref])))
            native = handoff.load(SCRIPT.parent / "decisionx_successor_native.py", "dx_native_integration")
            work = Path(directory) / "work"
            computed = native.run(first, [full], work)
            self.assertEqual(computed["input_zip_sha256s"], [first_sha, full_sha])
            instruction = ("Read the changed target state and implement the highest-value next "
                           "action, then independently verify its external effect and record uncertainty.")
            advice = {"schema": "cognilode.decisionx.successor_advice.v1",
                      "target_job_id": "target-1", "target_provider": "chatgpt.com",
                      "target_conversation_id": "current-1", "target_prompt_sha256": "e" * 64,
                      "target_response_sha256": "f" * 64, "candidate_refs": [candidate],
                      "proposed_next_instruction": instruction,
                      "changed_conditions": ["Target action completed"],
                      "uncertainties": ["Next effect remains unobserved"]}
            (work / "DECISIONX_ADVICE.json").write_bytes(handoff.canonical(advice))
            (work / "successor.plan").write_text(
                "project DecisionX Successor\nid decisionx-successor\n\n"
                "[ ] DX-1 Complete grounded next action\n"
                "    owner: Nadia Brooks\n    role: Research engineer\n"
                f"    do: {instruction}\n"
                "    done_when: The external target effect is independently observed with matching source identity.\n"
                "    effect_probe_command: /usr/bin/python3 -I /opt/cognilode/check.py read\n"
                "    effect_probe_expected: target effect present with source identity and matching digest\n")
            (work / "EXTERNAL_EFFECT_INSTRUCTIONS.md").write_text(
                "Install the TaskFlow plan through the named Codex employee, perform the external action, "
                "then independently reread the resulting state and check off only with effect evidence.")
            output = Path(directory) / "decisionx-successor-work-product.zip"
            sealed = native.finish(work, output)
            evidence = [{"kind": "chatgpt_sandbox_artifact", "ref": sealed["sha256"],
                         "path": str(output), "sha256": sealed["sha256"],
                         "mirrors": ["s3://private-test/" + sealed["sha256"]]}]
            validated = handoff.verify_output(post, {"decisionx": {"target_job_id": "target-1"}},
                                              evidence, hashes, sources, candidates, source_sha,
                                              historical_sha,
                                              stage_remote=lambda _url, _sha: output)
            self.assertEqual(validated[2], sealed["sha256"])
            central["provider_structured_uploads"].pop()
            with self.assertRaisesRegex(ValueError, "physical provider upload"):
                handoff.verify_inputs(post, job, central)

    def test_completed_chatmode_job_requires_native_exec_and_exact_central_report(self):
        job = {"id": "dx-1", "state": "complete", "provider": "chatgpt.com",
               "project": "DecisionX", "phase": "decisionx_successor",
               "reason": "source_linked_successor_from_verified_historical_IAE",
               "decisionx": {"reasoning_effort": "xhigh"},
               "rhythm_tape_sha256": "a" * 64, "conversation_id": "chat-1",
               "effect_evidence": [
                   {"kind": "provider_conversation", "ref": "chat-1"},
                   {"kind": "central_conversation_readback", "ref": "chat-1"},
                   {"kind": "provider_observed_native_exec", "raw_stream_sha256": "b" * 64}]}
        central = {"provider": "chatgpt.com", "conversation_id": "chat-1",
                   "events": [{"role": "assistant", "content": "A substantive provider report",
                               "source_content_complete": True}]}
        def post(body):
            if body["operation"] == "get_job":
                return {"ok": True, "job": job}
            if body["operation"] == "read":
                return {"ok": True, "conversation": central}
            raise AssertionError(body)
        self.assertEqual(handoff.provider_job(post, "dx-1"), (job, central))
        job["effect_evidence"].pop()
        with self.assertRaisesRegex(ValueError, "native"):
            handoff.provider_job(post, "dx-1")

    def test_output_zip_binds_uploaded_inputs_target_and_unchecked_plan(self):
        instruction = "Research the changed target context and execute the highest-value next action with measured external effect."
        source = {"provider": "openai-codex", "conversation_id": "old-1",
                  "prompt_sha256": "a" * 64, "response_sha256": "b" * 64,
                  "source_sha256": "c" * 64}
        candidate = {"segment_id": "decisionx.iae.123", **source}
        plan = ("project DecisionX successor\nid decisionx-successor\n\n"
                "[ ] S1 Act on the selected successor\n"
                "    owner: Nadia Brooks\n    role: Research engineer\n"
                f"    do: {instruction}\n"
                "    done_when: The real external effect is independently observed and source-linked.\n"
                "    effect_probe_command: /usr/bin/python3 -I /opt/cognilode/verify.py --read\n"
                "    effect_probe_expected: external effect present with exact source-linked identity\n").encode()
        advice = {"schema": "cognilode.decisionx.successor_advice.v1",
                  "target_job_id": "target-1", "target_provider": "openai-codex",
                  "target_conversation_id": "current-1", "target_prompt_sha256": "d" * 64,
                  "target_response_sha256": "e" * 64,
                  "candidate_refs": [candidate], "proposed_next_instruction": instruction,
                  "changed_conditions": ["The prior transport path has been repaired."],
                  "uncertainties": ["The next real external effect remains unobserved."]}
        input_hashes = ["1" * 64, "2" * 64]
        source_ref_hash = handoff.digest(handoff.canonical([source]))
        native = {"schema": "cognilode.decisionx.successor_native_compute.v1",
                  "input_zip_sha256s": input_hashes, "target_job_id": "target-1",
                  "candidate_count": 1, "source_count": 1,
                  "source_refs_sha256": source_ref_hash,
                  "historical_rendered_sha256": "1" * 64,
                  "target_rendered_sha256": handoff.digest(
                      ("# openai-codex / current-1").encode("utf-8"))}
        members = {"DECISIONX_ADVICE.json": handoff.canonical(advice),
                   "successor.plan": plan,
                   "EXTERNAL_EFFECT_INSTRUCTIONS.md": b"Deploy this work through the named Codex employee and independently reread the external effect. " * 2,
                   "NATIVE_COMPUTE.json": handoff.canonical(native)}
        manifest = {"schema": "cognilode.decisionx.successor_output.v1",
                    "members": {name: sha256(data).hexdigest() for name, data in members.items()}}
        target = {"provider": "openai-codex", "conversation_id": "current-1",
                  "prompt_sha256": "d" * 64, "response_sha256": "e" * 64}
        def post(body):
            if body["operation"] == "get_job":
                return {"ok": True, "job": {"id": "target-1", "state": "complete",
                                            "provider": "openai-codex", "conversation_id": "current-1"}}
            if body["operation"] == "read":
                return {"ok": True, "conversation": target}
            raise AssertionError(body)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisionx-successor-work-product.zip"
            archive_sha = write_zip(path, {"MANIFEST.json": handoff.canonical(manifest), **members})
            evidence = [{"kind": "chatgpt_sandbox_artifact", "ref": archive_sha,
                         "path": str(path), "sha256": archive_sha,
                         "mirrors": ["s3://private-test/" + archive_sha]}]
            job = {"id": "dx-1", "decisionx": {"target_job_id": "target-1"}}
            result = handoff.verify_output(post, job, evidence, input_hashes, [source],
                                           [candidate], source_ref_hash, "1" * 64,
                                           stage_remote=lambda _url, _sha: path)
            self.assertEqual(result[2], archive_sha)
            members["successor.plan"] = plan.replace(
                b"/usr/bin/python3 -I /opt/cognilode/verify.py --read", b"/usr/bin/true").replace(
                b"external effect present with exact source-linked identity", b"true")
            manifest["members"]["successor.plan"] = sha256(members["successor.plan"]).hexdigest()
            archive_sha = write_zip(path, {"MANIFEST.json": handoff.canonical(manifest), **members})
            evidence[0]["sha256"] = evidence[0]["ref"] = archive_sha
            evidence[0]["mirrors"] = ["s3://private-test/" + archive_sha]
            with self.assertRaisesRegex(ValueError, "pending owned steps"):
                handoff.verify_output(post, job, evidence, input_hashes, [source],
                                      [candidate], source_ref_hash, "1" * 64,
                                      stage_remote=lambda _url, _sha: path)
            members["successor.plan"] = plan
            manifest["members"]["successor.plan"] = sha256(plan).hexdigest()
            native["source_refs_sha256"] = "0" * 64
            members["NATIVE_COMPUTE.json"] = handoff.canonical(native)
            manifest["members"]["NATIVE_COMPUTE.json"] = sha256(members["NATIVE_COMPUTE.json"]).hexdigest()
            archive_sha = write_zip(path, {"MANIFEST.json": handoff.canonical(manifest), **members})
            evidence[0]["sha256"] = archive_sha
            evidence[0]["ref"] = archive_sha
            evidence[0]["mirrors"] = ["s3://private-test/" + archive_sha]
            with self.assertRaisesRegex(ValueError, "native compute proof"):
                handoff.verify_output(post, job, evidence, input_hashes, [source],
                                      [candidate], source_ref_hash, "1" * 64,
                                      stage_remote=lambda _url, _sha: path)

    def test_gate_rejects_index_below_500_even_when_stock_is_large(self):
        original = handoff.load
        class Root:
            @staticmethod
            def shared_stock_census(_post, minimum):
                return {"multi_year_ready": True, "distinct_complete": 900, "span_days": 800}
        handoff.load = lambda *_: Root()
        try:
            with self.assertRaisesRegex(ValueError, "descriptor index"):
                handoff.verify_shared_gates(lambda body: {"ok": True, "ready": False,
                                                           "distinct_sources": 499})
        finally:
            handoff.load = original

    def test_commits_plan_then_installs_existing_single_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "DecisionX Handoff Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            plan = ("project DecisionX successor\nid decisionx-successor\n\n"
                    "[ ] S1 Act on verified successor\n    owner: Nadia Brooks\n"
                    "    role: Research engineer\n").encode()
            seen = []
            def install(path, employees):
                seen.append((path, employees))
                return {"ok": True, "single_queue": "cloudflare-d1", "instance": "decisionx-successor"}
            job = {"id": "dx-1", "priority": 77, "decisionx": {
                "research_employee": "Nadia Brooks", "external_employee": "Rina Hale"}}
            first = handoff.commit_and_install(plan, job=job, output_root=repo / "plans", install=install)
            second = handoff.commit_and_install(plan, job=job, output_root=repo / "plans", install=install)
            self.assertEqual(first["plan_sha256"], sha256(plan).hexdigest())
            self.assertEqual(first["plan_path"], second["plan_path"])
            self.assertEqual(seen[0][1]["research_employee"], "Nadia Brooks")
            self.assertEqual(seen[0][1]["external_employee"], "Rina Hale")
            committed = subprocess.run(["git", "-C", str(repo), "show", "HEAD:plans/" + Path(first["plan_path"]).name],
                                       check=True, capture_output=True)
            self.assertEqual(committed.stdout, plan)

    def test_unattended_tick_distinguishes_provider_wait_from_terminal_failure(self):
        state = {"value": "queued"}
        def post(body):
            self.assertEqual(body["operation"], "get_job")
            return {"ok": True, "job": {"id": "dx-1", "state": state["value"]}}
        with tempfile.TemporaryDirectory() as directory:
            pending = handoff.tick(post, job_id="dx-1", output_root=Path(directory),
                                   state_root=Path(directory) / "state")
            self.assertEqual(pending["phase"], "waiting_provider")
            self.assertTrue(pending["ok"])
            state["value"] = "failed"
            failed = handoff.tick(post, job_id="dx-1", output_root=Path(directory),
                                  state_root=Path(directory) / "state")
            self.assertEqual(failed["phase"], "predecessor_failed")
            self.assertFalse(failed["ok"])


if __name__ == "__main__":
    unittest.main()

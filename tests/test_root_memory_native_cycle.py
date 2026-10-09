from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-root-memory-native-cycle"
loader = importlib.machinery.SourceFileLoader("root_memory_cycle_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
cycle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cycle
loader.exec_module(cycle)


class FakeBridge:
    def __init__(self, refs):
        self.refs = refs

    def shared_stock_census(self, post, minimum=500):
        return {"multi_year_ready": len(self.refs) >= minimum,
                "distinct_complete": len(self.refs), "span_days": 800,
                "verified_source_refs": self.refs}

    def _complete_source(self, index, read):
        row = read.get("conversation") or {}
        return datetime.now(timezone.utc) if row.get("capture", {}).get("drive_verified") else None


class FakeD1:
    def __init__(self, refs):
        self.refs = refs
        self.enqueued = []
        self.jobs = {}
        self.chat = {}

    def __call__(self, request):
        if request["operation"] == "read":
            cid = request["conversation_id"]
            if request["provider"] == "chatgpt.com":
                return {"ok": True, "conversation": self.chat[cid]}
            ref = next(r for r in self.refs if r["conversation_id"] == cid)
            return {"ok": True, "conversation": {"provider": ref["provider"],
                "conversation_id": cid, "prompt_sha256": ref["prompt_sha256"],
                "response_sha256": ref["response_sha256"],
                "capture": {"drive_verified": True},
                "events": [{"role": "user", "content": "Source task " + cid},
                           {"role": "assistant", "content": "Source result " + cid}]}}
        if request["operation"] == "enqueue_job":
            self.enqueued.append(request)
            return {"ok": True, "job": {**request, "state": "queued"}}
        if request["operation"] == "get_job":
            return {"ok": True, "job": self.jobs[request["job_id"]]}
        raise AssertionError(request["operation"])


class FakeController:
    def parse_plan(self, path):
        text = path.read_text()
        root = path == cycle.ROOT_PLAN
        return type("Plan", (), {"text": text, "sha256": sha256(text.encode()).hexdigest(),
                                  "steps": [type("Step", (), {"step_id": "ROOT-PLAN" if root else "MEMORY-BUILD"})()],
                                  "project_id": "root-agent" if root else "root-memory"})()

    def verify_plan_revision(self, plan):
        return None

    def job_base(self, plan, step, phase, *, provider, priority, dependencies):
        return {"id": "base", "provider": provider, "state": "queued", "project": plan.project_id,
                "taskflow_step": step.step_id, "phase": phase,
                "plan_revision": plan.sha256, "plan_text": plan.text,
                "plan_source_ref": "file:" + str(cycle.PLAN), "priority": priority}


def refs(count=500):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [{"provider": "openai-codex", "conversation_id": f"c{i:04d}",
             "prompt_sha256": "a" * 64, "response_sha256": "b" * 64,
             "source_at_utc": (start + timedelta(days=round(i * 800 / (count - 1)))).isoformat()}
            for i in range(count)]


class RootMemoryCycleTests(unittest.TestCase):
    def test_tick_advances_native_root_secretary_without_direct_provider_send(self):
        d1 = FakeD1([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_calls, finalize_calls, handoff_calls = [], [], []
            def stage(_post, **kwargs):
                prepare_calls.append(kwargs)
                return {"ok": True, "job": {"job_id": "native-1"},
                        "source_set_sha256": "a" * 64, "provider_requests_created": 0}
            def finish(_post, **kwargs):
                finalize_calls.append(kwargs)
                return {"ok": True, "root_job": {"job_id": "root-1"},
                        "provider_requests_created": 0}
            def handoff(_post, **kwargs):
                handoff_calls.append(kwargs)
                return {"ok": True, "plan_sha256": "b" * 64,
                        "secretary_taskflow_installation": {"ok": True}}
            kwargs = {"output_root": root, "plan_output_root": root / "plans",
                      "prepare_fn": stage, "finalize_fn": finish, "handoff_fn": handoff}
            self.assertEqual(cycle.tick(d1, **kwargs)["phase"], "await_native")
            d1.jobs["native-1"] = {"state": "queued"}
            self.assertEqual(cycle.tick(d1, **kwargs)["job_state"], "queued")
            d1.jobs["native-1"] = {"state": "complete"}
            self.assertEqual(cycle.tick(d1, **kwargs)["phase"], "await_root")
            d1.jobs["root-1"] = {"state": "complete"}
            self.assertEqual(cycle.tick(d1, **kwargs)["phase"], "complete")
            self.assertEqual(cycle.tick(d1, **kwargs)["phase"], "complete")
            self.assertEqual(len(prepare_calls), 1)
            self.assertEqual(len(finalize_calls), 1)
            self.assertEqual(len(handoff_calls), 1)
            self.assertEqual(json.loads((root / "cycle-state.json").read_text())["phase"], "complete")

    def test_500_full_sources_are_batched_and_reconstructed_in_native_script(self):
        selected = cycle.source_selection(FakeBridge(refs()).shared_stock_census(None))
        self.assertEqual(len(selected), 500)
        self.assertEqual(selected[0]["conversation_id"], "c0000")
        self.assertEqual(selected[-1]["conversation_id"], "c0499")
        d1 = FakeD1(selected)
        with tempfile.TemporaryDirectory() as directory:
            batches = cycle.build_source_batches(d1, selected, output_root=Path(directory), bridge=FakeBridge(selected))
            self.assertTrue(batches)
            self.assertTrue(all(row["bytes"] <= cycle.MAX_ZIP_BYTES for row in batches))
            native = Path(directory) / "native"
            native.mkdir()
            for index, row in enumerate(batches):
                folder = native / f"batch-{index:03d}"
                folder.mkdir()
                with zipfile.ZipFile(row["path"]) as archive:
                    self.assertIsNone(archive.testzip())
                    archive.extractall(folder)
            script = native / "batch-000" / "RECONSTRUCT_IN_NATIVE_SANDBOX.py"
            output = subprocess.run([sys.executable, str(script)], cwd=native,
                                    capture_output=True, text=True, check=True)
            self.assertIn("reconstructed_distinct_sources 500", output.stdout)
            lines = (native / "reconstructed_sources.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 500)
            self.assertEqual(json.loads(lines[0])["conversation_id"], "c0000")

    def test_no_under_500_source_packet_or_job(self):
        partial = refs(2)
        d1 = FakeD1(partial)
        with tempfile.TemporaryDirectory() as directory:
            result = cycle.prepare(d1, output_root=Path(directory), bridge=FakeBridge(partial),
                                   controller=FakeController())
            self.assertFalse(result["ok"])
            self.assertEqual(d1.enqueued, [])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_job_enqueued_with_all_physical_zips_and_xhigh_reasoning(self):
        selected = refs()
        d1 = FakeD1(selected)
        with tempfile.TemporaryDirectory() as directory:
            result = cycle.prepare(d1, output_root=Path(directory), bridge=FakeBridge(selected),
                controller=FakeController(), publish=lambda row: "s3://private/taskflow-artifacts/sha256/" + row["sha256"] + ".zip")
            self.assertTrue(result["ok"])
            self.assertEqual(len(d1.enqueued), 1)
            job = d1.enqueued[0]
            self.assertEqual(job["provider"], "chatgpt.com")
            self.assertEqual(job["reasoning_effort"], "xhigh")
            self.assertEqual(job["model"], "gpt-5-6-thinking")
            self.assertEqual(job["prompt_authority"], "taskflow_plan")
            self.assertEqual(len(job["attachment_refs"]), result["batch_count"])
            self.assertTrue(all(row["ref"].startswith("file:") and row["mirrors"]
                                for row in job["attachment_refs"]))
            self.assertEqual(result["provider_requests_created"], 0)

    def test_native_output_is_verified_before_root_job_is_queued(self):
        selected = refs()
        d1 = FakeD1(selected)
        with tempfile.TemporaryDirectory() as directory:
            batches = cycle.build_source_batches(d1, selected, output_root=Path(directory) / "inputs",
                bridge=FakeBridge(selected))
            packet = Path(directory) / "native-result.zip"
            bodies = {key: ((key + " multi-year source-grounded synthesis. ") * 55).encode()
                      for key in ("global", "project", "role")}
            manifest = {"schema": "cognilode.root_memory_packet.v1",
                        "source_refs": selected,
                        "sections": {key: {"path": key + ".md", "sha256": sha256(body).hexdigest()}
                                     for key, body in bodies.items()}}
            with zipfile.ZipFile(packet, "w") as archive:
                archive.writestr("MANIFEST.json", json.dumps(manifest))
                archive.writestr("EXTERNAL_EFFECT_INSTRUCTIONS.md", "Publish then prompt root.")
                for key, body in bodies.items():
                    archive.writestr(key + ".md", body)
            digest = sha256(packet.read_bytes()).hexdigest()
            d1.jobs["native-job"] = {"id": "native-job", "provider": "chatgpt.com",
                "state": "complete", "phase": "chatgpt_sandbox", "taskflow_step": "MEMORY-BUILD",
                "attachment_refs": [{"ref": "file:" + row["path"], "sha256": row["sha256"]}
                                    for row in batches],
                "conversation_id": "chat-native", "effect_evidence": [
                    {"kind": "provider_observed_functions_exec", "ref": "native-tool-result"},
                    {"kind": "provider_conversation", "ref": "chat-native"},
                    {"kind": "central_conversation_readback", "ref": "chat-native"},
                    {"kind": "chatgpt_sandbox_artifact", "ref": digest, "path": str(packet)}]}
            d1.chat["chat-native"] = {"conversation_id": "chat-native",
                "provider_structured_uploads": [{"sha256": row["sha256"]} for row in batches],
                "events": [{"role": "assistant", "content": "Native report and results."}]}
            result = cycle.finalize(d1, native_job_id="native-job", bridge=FakeBridge(selected),
                controller=FakeController(), publish=lambda row: "s3://private/taskflow-artifacts/sha256/" + row["sha256"] + ".zip")
            self.assertTrue(result["ok"])
            self.assertEqual(result["source_count"], 500)
            self.assertEqual(len(d1.enqueued), 1)
            root = d1.enqueued[0]
            self.assertEqual(root["project"], "root-agent")
            self.assertEqual(root["taskflow_step"], "ROOT-PLAN")
            self.assertEqual(root["attachment_refs"][0]["sha256"], digest)
            self.assertEqual(root["reasoning_effort"], "xhigh")
            self.assertEqual(result["provider_requests_created"], 0)
            d1.chat["chat-native"]["provider_structured_uploads"] = []
            with self.assertRaisesRegex(ValueError, "structured upload proof"):
                cycle.finalize(d1, native_job_id="native-job", bridge=FakeBridge(selected),
                               controller=FakeController(), publish=lambda row: "not-used")
            d1.chat["chat-native"]["provider_structured_uploads"] = [
                {"sha256": row["sha256"]} for row in batches]
            d1.jobs["native-job"]["effect_evidence"] = [row for row in d1.jobs["native-job"]["effect_evidence"]
                if row["kind"] != "provider_observed_functions_exec"]
            with self.assertRaises(ValueError):
                cycle.finalize(d1, native_job_id="native-job", bridge=FakeBridge(selected),
                               controller=FakeController(), publish=lambda row: "not-used")


if __name__ == "__main__":
    unittest.main()

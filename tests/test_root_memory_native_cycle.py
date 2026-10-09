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
from unittest import mock
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

    def shared_stock_census(self, post, minimum=500, **_kwargs):
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
    def test_successful_native_handoff_retires_only_transient_source_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_root = root / "source-set"
            batch_root.mkdir()
            cache = batch_root / ".verified-sources"
            cache.mkdir()
            (cache / "source.json").write_text("derived source packet")
            (root / "verified-selection.json").write_text("derived selection")
            source = root / "original-source.jsonl"
            source.write_text("original source remains")
            packet = batch_root / "batch-000.zip"
            packet.write_bytes(b"physical source packet")
            job = {"attachment_refs": [{"ref": "file:" + str(packet),
                                        "sha256": sha256(packet.read_bytes()).hexdigest(),
                                        "mirrors": ["s3://private/content-addressed"]}]}
            self.assertEqual(cycle.retire_local_source_staging(job, output_root=root), 1)
            self.assertFalse(packet.exists())
            self.assertFalse(cache.exists())
            self.assertFalse((root / "verified-selection.json").exists())
            self.assertEqual(source.read_text(), "original source remains")

    def test_private_source_mirror_requires_full_remote_sha_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = Path(directory) / "source.zip"
            packet.write_bytes(b"verified physical ZIP bytes")
            digest = sha256(packet.read_bytes()).hexdigest()
            batch = {"path": str(packet), "sha256": digest}

            def fake_run(args, **_kwargs):
                if "head-object" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
                        "ContentLength": packet.stat().st_size, "Metadata": {"sha256": digest}}))
                if "get-object" in args:
                    Path(args[-1]).write_bytes(b"verified physical ZIP bytes")
                return subprocess.CompletedProcess(args, 0, stdout="")

            with mock.patch.object(cycle.subprocess, "run", side_effect=fake_run):
                mirror = cycle.publish_private(batch, bucket="test-bucket", aws="fake-aws")
            self.assertIn(digest, mirror)

            def corrupt_run(args, **_kwargs):
                if "get-object" in args:
                    Path(args[-1]).write_bytes(b"corrupted remote source ZIP")
                    return subprocess.CompletedProcess(args, 0, stdout="")
                return fake_run(args, **_kwargs)

            with mock.patch.object(cycle.subprocess, "run", side_effect=corrupt_run):
                with self.assertRaisesRegex(ValueError, "full-byte readback mismatch"):
                    cycle.publish_private(batch, bucket="test-bucket", aws="fake-aws")

    def test_source_packet_resumes_exact_verified_reads_after_transient_failure(self):
        selected = cycle.source_selection(FakeBridge(refs()).shared_stock_census(None))
        d1 = FakeD1(selected)
        calls = []
        fail = True

        def flaky(body):
            nonlocal fail
            if body["operation"] == "read":
                calls.append(body["conversation_id"])
                if body["conversation_id"] == "c0250" and fail:
                    fail = False
                    raise RuntimeError("Agent Memory HTTP 403: simulated nonretryable failure")
            return d1(body)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                cycle.build_source_batches(flaky, selected, output_root=root, bridge=FakeBridge(selected))
            self.assertEqual(calls[0], "c0000")
            self.assertEqual(calls[-1], "c0250")
            calls.clear()
            batches = cycle.build_source_batches(flaky, selected, output_root=root, bridge=FakeBridge(selected))
            self.assertTrue(batches)
            self.assertEqual(calls[0], "c0250")
            self.assertEqual(calls[-1], "c0499")
            cache_path = next((root / ".verified-sources").glob("*.json"))
            if cache_path.name.endswith(".receipt.json"):
                cache_path = next(path for path in (root / ".verified-sources").glob("*.json")
                                  if not path.name.endswith(".receipt.json"))
            cache_path.write_bytes(b"corrupted")
            calls.clear()
            cycle.build_source_batches(flaky, selected, output_root=root, bridge=FakeBridge(selected))
            self.assertEqual(len(calls), 1)

    def test_bad_native_deliverable_gets_delayed_successor_on_same_rhythm_queue(self):
        d1 = FakeD1([])
        attempts = []
        def stage(_post, **kwargs):
            attempt = kwargs.get("attempt", 0)
            attempts.append(attempt)
            return {"ok": True, "job": {"job_id": f"native-a{attempt}"},
                    "source_set_sha256": "a" * 64}
        def bad_output(_post, **_kwargs):
            raise ValueError("completed job had no native work ZIP")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kwargs = {"output_root": root, "plan_output_root": root / "plans",
                      "prepare_fn": stage, "finalize_fn": bad_output}
            self.assertEqual(cycle.tick(d1, **kwargs)["native_job_id"], "native-a0")
            d1.jobs["native-a0"] = {"state": "complete"}
            delayed = cycle.tick(d1, **kwargs)
            self.assertFalse(delayed["ok"])
            self.assertEqual(delayed["reason"], "native_deliverable_retry_delayed")
            self.assertEqual(attempts, [0])
            state_path = root / "cycle-state.json"
            state = json.loads(state_path.read_text())
            state["retry_after_epoch"] = 1
            state_path.write_text(json.dumps(state))
            successor = cycle.tick(d1, **kwargs)
            self.assertEqual(successor["native_job_id"], "native-a1")
            self.assertEqual(successor["replaced_bad_job_id"], "native-a0")
            self.assertEqual(attempts, [0, 1])
            self.assertEqual(successor["provider_requests_created"], 0)

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
                        "plan_path": str(root / "plans" / "root.plan"),
                        "root_memory_packet_sha256": "c" * 64,
                        "secretary_taskflow_installation": {"ok": True}}
            kwargs = {"output_root": root, "plan_output_root": root / "plans",
                      "prepare_fn": stage, "finalize_fn": finish, "handoff_fn": handoff}
            self.assertEqual(cycle.tick(d1, **kwargs)["phase"], "await_native")
            d1.jobs["native-1"] = {"state": "queued"}
            self.assertEqual(cycle.tick(d1, **kwargs)["job_state"], "queued")
            staged = root / "batch-000.zip"
            staged.write_bytes(b"transient-source-packet")
            d1.jobs["native-1"] = {"state": "complete", "attachment_refs": [{
                "ref": "file:" + str(staged), "sha256": sha256(staged.read_bytes()).hexdigest(),
                "mirrors": ["s3://private/exact-source.zip"]}]}
            self.assertEqual(cycle.tick(d1, **kwargs)["phase"], "await_root")
            self.assertFalse(staged.exists())
            d1.jobs["root-1"] = {"state": "complete"}
            self.assertEqual(cycle.tick(d1, **kwargs)["phase"], "complete")
            self.assertEqual(cycle.tick(d1, **kwargs)["phase"], "complete")
            self.assertEqual(len(prepare_calls), 1)
            self.assertEqual(len(finalize_calls), 1)
            self.assertEqual(len(handoff_calls), 1)
            completed = json.loads((root / "cycle-state.json").read_text())
            self.assertEqual(completed["phase"], "complete")
            self.assertEqual(completed["plan_path"], str(root / "plans" / "root.plan"))
            self.assertEqual(completed["root_memory_packet_sha256"], "c" * 64)

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
            for key in ("global", "project", "role"):
                (native / (key + ".md")).write_text((key + " source-grounded synthesis. ") * 100)
            (native / "EXTERNAL_EFFECT_INSTRUCTIONS.md").write_text("Use the root memory as context for company plans.")
            packet_builder = native / "batch-000" / "BUILD_MEMORY_PACKET_IN_NATIVE_SANDBOX.py"
            subprocess.run([sys.executable, str(packet_builder)], cwd=native,
                           capture_output=True, text=True, check=True)
            with zipfile.ZipFile(native / "root-memory-work-product.zip") as packet:
                manifest = json.loads(packet.read("MANIFEST.json"))
                self.assertEqual(manifest["schema"], "cognilode.root_memory_packet.v1")
                self.assertEqual(len(manifest["source_refs"]), 500)
                self.assertEqual(set(manifest["sections"]), {"global", "project", "role"})

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
                    {"kind": "provider_observed_native_exec", "ref": "native-tool-result"},
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
                if row["kind"] != "provider_observed_native_exec"]
            d1.jobs["native-job"]["effect_evidence"].append(
                {"kind": "provider_observed_functions_exec", "ref": "code-mode-tool-result"})
            with self.assertRaises(ValueError):
                cycle.finalize(d1, native_job_id="native-job", bridge=FakeBridge(selected),
                               controller=FakeController(), publish=lambda row: "not-used")


if __name__ == "__main__":
    unittest.main()

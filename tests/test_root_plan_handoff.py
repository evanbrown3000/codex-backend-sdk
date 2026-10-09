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


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-root-plan-handoff"
loader = importlib.machinery.SourceFileLoader("root_plan_handoff_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
loader.exec_module(bridge)


def full(provider: str, cid: str, day: int) -> dict:
    at = (datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)).isoformat()
    return {"provider": provider, "conversation_id": cid,
            "prompt_sha256": "a" * 64, "response_sha256": "b" * 64,
            "capture": {"drive_verified": True, "source_complete": True, "source_response_complete": True,
                        "source_sha256": "c" * 64, "drive_source_sha256": "c" * 64,
                        "drive_object_sha256": "d" * 64, "drive_verified_at": at,
                        "drive_conversation_id": cid},
            "events": [{"role": "user", "content": "plan this", "created_at": at,
                        "source_content_complete": True},
                       {"role": "assistant", "content": "implemented it", "created_at": at,
                        "source_content_complete": True}]}


class FakeD1:
    def __init__(self, rows, sources, job=None, root_conversation=None):
        self.rows = rows
        self.sources = sources
        self.job = job or {}
        self.root_conversation = root_conversation or {}
        self.read_calls = []

    def __call__(self, body):
        op = body["operation"]
        if op == "conversations":
            start = int(body["cursor"])
            page = self.rows[start:start + body["limit"]]
            return {"ok": True, "records": page,
                    "next_cursor": str(start + len(page)) if start + len(page) < len(self.rows) else None}
        if op == "read":
            key = (body["provider"], body["conversation_id"])
            self.read_calls.append(key)
            if key == ("chatgpt.com", self.job.get("conversation_id")):
                return {"ok": True, "conversation": self.root_conversation}
            return {"ok": True, "conversation": self.sources[key]}
        if op == "get_job":
            return {"ok": True, "job": self.job}
        raise AssertionError(op)


class RootPlanHandoffTests(unittest.TestCase):
    def test_drive_complete_source_accepts_interim_flags_and_flagless_snapshot(self):
        source = full("openai-codex", "interim", 0)
        source["events"][0]["source_content_complete"] = False
        source["events"].insert(1, {"role": "assistant", "content": "interim summary",
                                     "source_content_complete": False})
        self.assertIsNotNone(bridge._complete_source(source, {"ok": True, "conversation": source}))
        source["events"][-1]["source_content_complete"] = False
        self.assertIsNone(bridge._complete_source(source, {"ok": True, "conversation": source}))
        for event in source["events"]:
            event.pop("source_content_complete", None)
        self.assertIsNotNone(bridge._complete_source(source, {"ok": True, "conversation": source}))
        source["capture"].pop("source_complete")
        self.assertIsNone(bridge._complete_source(source, {"ok": True, "conversation": source}))

    def test_stock_counts_500_distinct_complete_sources_across_years(self):
        rows = []
        sources = {}
        for i in range(500):
            cid = f"c{i}"
            row = full("openai-codex", cid, round(i * 800 / 499))
            rows.append({"provider": "openai-codex", "conversation_id": cid,
                         "capture": row["capture"]})
            sources[("openai-codex", cid)] = row
        d1 = FakeD1(rows, sources)
        count = bridge.shared_stock_census(d1)
        self.assertTrue(count["multi_year_ready"])
        self.assertEqual(count["distinct_complete"], 500)
        rows.append(rows[0])  # duplicate identity must not count twice
        sources[("openai-codex", "c499")]["capture"]["source_response_complete"] = False
        self.assertFalse(bridge.shared_stock_census(d1)["multi_year_ready"])

    def test_root_plan_is_preserved_as_taskflow_instruction(self):
        plan = {"schema": "cognilode.root_taskflow_plan.v1", "project_id": "memory-a",
                "project_name": "Long Horizon Memory", "research_employee": "Nadia Brooks",
                "external_employee": "Rina Hale", "steps": [
                    {"id": "M1", "title": "Recover early work", "owner": "Nadia Brooks",
                     "role": "Knowledge Engineer", "instructions": ["Compare all 2024 and 2025 source conversations."],
                     "depends_on": [], "effect_probe": {"command": "/usr/bin/python3 -I /tmp/probe.py evidence", "expected": "live"}},
                    {"id": "M2", "title": "Apply project memory", "owner": "Rina Hale",
                     "role": "Integration Engineer", "instructions": ["Deploy the selected memory layer."],
                     "depends_on": ["M1"], "effect_probe": {"command": "/usr/bin/python3 -I /tmp/probe.py outcome", "expected": "live"}}]}
        response = "Report\n" + bridge.BEGIN + "\n" + json.dumps(plan) + "\n" + bridge.END
        job = {"id": "root-job", "state": "complete", "provider": "chatgpt.com",
               "project": "root-agent", "conversation_id": "root-chat", "rhythm_tape_sha256": "c" * 64,
               "effect_evidence": [{"kind": "provider_conversation", "ref": "root-chat"},
                                   {"kind": "central_conversation_readback", "ref": "root-chat"}]}
        central = {"conversation_id": "root-chat", "events": [
            {"role": "assistant", "content": response, "source_content_complete": True}]}
        parsed, source_sha = bridge.extract_plan(job, central)
        self.assertEqual(parsed, plan)
        rendered = bridge.render_taskflow(parsed, conversation_id="root-chat", assistant_sha256=source_sha)
        self.assertIn("      - Compare all 2024 and 2025 source conversations.", rendered)
        self.assertIn("    depends_on: M1", rendered)
        self.assertNotIn("write a prompt", rendered)
        bad = json.loads(json.dumps(plan))
        bad["steps"][0]["effect_probe"]["command"] = "/usr/bin/true"
        with self.assertRaises(ValueError):
            bridge.render_taskflow(bad, conversation_id="root-chat", assistant_sha256=source_sha)
        bad["steps"][0]["effect_probe"]["command"] = "/usr/bin/python3 -I /tmp/probe.py evidence\n[ ] injected fake"
        with self.assertRaises(ValueError):
            bridge.render_taskflow(bad, conversation_id="root-chat", assistant_sha256=source_sha)
        broken = dict(job, effect_evidence=[])
        with self.assertRaises(ValueError):
            bridge.extract_plan(broken, central)

    def test_handoff_does_not_write_or_send_from_tiny_stock(self):
        row = full("openai-codex", "only-one", 0)
        d1 = FakeD1([{"provider": "openai-codex", "conversation_id": "only-one",
                      "capture": row["capture"]}], {("openai-codex", "only-one"): row})
        with tempfile.TemporaryDirectory() as directory:
            result = bridge.handoff(d1, job_id="root-job", output_root=Path(directory))
            self.assertEqual(result["reason"], "shared_multi_year_stock_incomplete")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_verified_root_plan_installs_existing_secretary_taskflow_controller(self):
        rows, sources = [], {}
        for i in range(2):
            cid = f"source-{i}"
            source = full("openai-codex", cid, 800 * i)
            rows.append({"provider": "openai-codex", "conversation_id": cid,
                         "capture": source["capture"]})
            sources[("openai-codex", cid)] = source
        plan = {"schema": "cognilode.root_taskflow_plan.v1", "project_id": "root-project",
                "project_name": "Root Project", "research_employee": "Nadia Brooks",
                "external_employee": "Rina Hale", "steps": [{"id": "A", "title": "Apply plan",
                    "owner": "Nadia Brooks", "role": "Knowledge Engineer",
                    "instructions": ["Carry out the manager plan literally."], "depends_on": [],
                    "effect_probe": {"command": "/usr/bin/python3 -I /tmp/probe.py evidence",
                                     "expected": "live"}}]}
        report = bridge.BEGIN + "\n" + json.dumps(plan) + "\n" + bridge.END
        job = {"id": "root-job", "state": "complete", "provider": "chatgpt.com",
               "project": "root-agent", "conversation_id": "root-chat", "rhythm_tape_sha256": "c" * 64,
               "effect_evidence": [{"kind": "provider_conversation", "ref": "root-chat"},
                                   {"kind": "central_conversation_readback", "ref": "root-chat"}]}
        central = {"conversation_id": "root-chat", "events": [
            {"role": "assistant", "content": report, "source_content_complete": True}]}
        d1 = FakeD1(rows, sources, job, central)
        installed = []
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            subprocess.run(["git", "-C", directory, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", directory, "config", "user.email", "test@example.invalid"], check=True)
            (Path(directory) / "README.md").write_text("Project\n")
            subprocess.run(["git", "-C", directory, "add", "README.md"], check=True)
            subprocess.run(["git", "-C", directory, "commit", "-qm", "initial"], check=True)
            refs = bridge.shared_stock_census(d1, minimum=2)["verified_source_refs"]
            packet = Path(directory) / "root-memory.zip"
            bodies = {"global": b"Global history spanning many years. " * 8,
                      "project": b"Project-specific memory with source links. " * 8,
                      "role": b"Role-specific prior outcomes and lessons. " * 8}
            manifest = {"schema": "cognilode.root_memory_packet.v1", "source_refs": refs,
                        "sections": {key: {"path": key + ".md", "sha256": sha256(body).hexdigest()}
                                     for key, body in bodies.items()}}
            with zipfile.ZipFile(packet, "w") as archive:
                archive.writestr("MANIFEST.json", json.dumps(manifest))
                for key, body in bodies.items():
                    archive.writestr(key + ".md", body)
            packet_sha = sha256(packet.read_bytes()).hexdigest()
            job["attachment_refs"] = [{"ref": "file:" + str(packet), "sha256": packet_sha}]
            central["provider_structured_uploads"] = [{"sha256": packet_sha}]
            def install(path, value):
                installed.append((path, value))
                return {"ok": True, "single_queue": "cloudflare-d1"}
            result = bridge.handoff(d1, job_id="root-job", output_root=Path(directory),
                                    minimum=2, install=install)
            self.assertTrue(result["ok"])
            self.assertEqual(len(installed), 1)
            self.assertEqual(installed[0][1], plan)
            self.assertTrue(installed[0][0].is_file())
            self.assertIn("Carry out the manager plan literally.", installed[0][0].read_text())
            source = subprocess.run(["git", "-C", directory, "show", "HEAD:root-project.plan"],
                                    check=True, capture_output=True, text=True)
            self.assertEqual(source.stdout, installed[0][0].read_text())
            self.assertEqual(result["provider_requests_created"], 0)
            self.assertEqual(result["root_memory_packet_sha256"], packet_sha)
            job["attachment_refs"][0]["ref"] = "file:/missing/on/other-device/root-memory.zip"
            mirror = "s3://private-bucket/taskflow-artifacts/sha256/" + packet_sha + ".zip"
            job["attachment_refs"][0]["mirrors"] = [mirror]
            used = []
            staged = bridge.verify_root_memory_packet(job, central,
                bridge.shared_stock_census(d1, minimum=2), minimum=2,
                stage_remote=lambda url, digest: (used.append((url, digest)) or packet))
            self.assertEqual(staged, packet_sha)
            self.assertEqual(used, [(mirror, packet_sha)])
            central["provider_structured_uploads"] = []
            with self.assertRaises(ValueError):
                bridge.handoff(d1, job_id="root-job", output_root=Path(directory),
                               minimum=2, install=install)


if __name__ == "__main__":
    unittest.main()

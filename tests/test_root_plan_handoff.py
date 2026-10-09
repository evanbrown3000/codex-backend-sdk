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

    def test_export_and_live_chatgpt_alias_count_as_one_logical_conversation(self):
        cid = "29c2e8d8-1270-4efd-9546-f2a8bf4ce5f1"
        live = full("chatgpt.com", cid, 0)
        exported = full("chatgpt-export-format", cid, 0)
        sources = {("chatgpt.com", cid): live,
                   ("chatgpt-export-format", cid): exported}
        rows = [{"provider": provider, "conversation_id": cid,
                 "capture": source["capture"]}
                for (provider, _), source in sources.items()]
        d1 = FakeD1(rows, sources)
        census = bridge.shared_stock_census(d1, minimum=2)
        self.assertEqual(census["index_identities_seen"], 2)
        self.assertEqual(census["distinct_complete"], 1)
        self.assertEqual(len(census["verified_source_refs"]), 1)
        self.assertFalse(census["multi_year_ready"])

        # An incomplete live row must not suppress the verified export row.
        live["capture"]["drive_verified"] = False
        rows[0]["capture"] = live["capture"]
        census = bridge.shared_stock_census(FakeD1(rows, sources), minimum=1)
        self.assertEqual(census["distinct_complete"], 1)
        self.assertEqual(census["verified_source_refs"][0]["provider"],
                         "chatgpt-export-format")

    def test_stock_readback_checkpoint_recovers_after_one_worker_exception(self):
        sources = {('openai-codex', f'c{i}'): full('openai-codex', f'c{i}', i * 400)
                   for i in range(3)}
        rows = [{'provider': provider, 'conversation_id': cid,
                 'updated_at': f'2026-10-09T11:00:0{i}Z',
                 'capture': source['capture']}
                for i, ((provider, cid), source) in enumerate(sources.items())]

        class FlakyD1(FakeD1):
            fail = True

            def __call__(self, body):
                if (body['operation'] == 'read' and body['conversation_id'] == 'c1'
                    and self.fail):
                    self.read_calls.append((body['provider'], body['conversation_id']))
                    raise RuntimeError('intermittent D1 Worker 1101')
                return super().__call__(body)

        d1 = FlakyD1(rows, sources)
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / 'readback-proofs.json'
            first = bridge.shared_stock_census(d1, minimum=3, checkpoint_path=checkpoint)
            self.assertEqual(first['distinct_complete'], 2)
            self.assertEqual(first['partial_read_errors'], 1)
            self.assertTrue(checkpoint.is_file())
            d1.fail = False
            second = bridge.shared_stock_census(d1, minimum=3, checkpoint_path=checkpoint)
            self.assertEqual(second['distinct_complete'], 3)
            self.assertTrue(second['multi_year_ready'])
            self.assertEqual(d1.read_calls.count(('openai-codex', 'c0')), 1)
            self.assertEqual(d1.read_calls.count(('openai-codex', 'c2')), 1)
            rows[0]['updated_at'] = '2026-10-09T12:00:00Z'
            bridge.shared_stock_census(d1, minimum=3, checkpoint_path=checkpoint)
            self.assertEqual(d1.read_calls.count(('openai-codex', 'c0')), 2)

    def test_decisionx_stock_stops_once_verified_minimum_spans_two_years(self):
        rows = []
        sources = {}
        for i, day in enumerate((0, 800, 900)):
            cid = f'c{i}'
            source = full('openai-codex', cid, day)
            rows.append({'provider': 'openai-codex', 'conversation_id': cid,
                         'capture': source['capture']})
            sources[('openai-codex', cid)] = source
        d1 = FakeD1(rows, sources)
        result = bridge.shared_stock_census(d1, minimum=2, stop_at_minimum=True)
        self.assertEqual(result['distinct_complete'], 2)
        self.assertTrue(result['multi_year_ready'])
        self.assertFalse(result['scan_complete'])
        self.assertNotIn(('openai-codex', 'c2'), d1.read_calls)

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

    def test_root_portfolio_preserves_company_team_employee_instruction_edges(self):
        probe = {"command": "/usr/bin/python3 -I /tmp/probe.py evidence", "expected": "live"}
        plan = {"schema": "cognilode.root_taskflow_portfolio.v1",
                "project_id": "living-company", "project_name": "Living Company",
                "research_employee": "Nadia Brooks", "external_employee": "Rina Hale",
                "scopes": [
                    {"id": "company", "kind": "company", "name": "Company", "parent": None},
                    {"id": "memory-team", "kind": "team", "name": "Memory Team", "parent": "company"},
                    {"id": "eli", "kind": "employee", "name": "Eli's assignment",
                     "parent": "memory-team", "employee": "Eli Rowan"}],
                "steps": [
                    {"id": "C1", "scope_id": "company", "title": "Choose multi-year direction",
                     "owner": "Root Manager", "role": "Company Manager", "depends_on": [],
                     "instructions": ["Set a company direction from multi-year evidence."], "effect_probe": probe},
                    {"id": "T1", "scope_id": "memory-team", "title": "Plan the memory team",
                     "owner": "Nadia Brooks", "role": "Team Manager", "depends_on": ["C1"],
                     "instructions": ["Translate the company direction into a team plan."], "effect_probe": probe},
                    {"id": "E1", "scope_id": "eli", "title": "Execute the memory task",
                     "owner": "Eli Rowan", "role": "Memory Engineer", "depends_on": ["T1"],
                     "instructions": ["Use the team's source analysis as instruction."], "effect_probe": probe}]}
        text = bridge.render_taskflow(plan, conversation_id="root-chat", assistant_sha256="e" * 64)
        parsed = bridge._load(bridge.CONTROLLER, "test_root_hierarchy_controller").parse_plan_text(text)
        self.assertEqual(parsed.by_id()["T1"].dependencies, ("C1",))
        self.assertEqual(parsed.by_id()["E1"].dependencies, ("T1",))
        self.assertEqual(parsed.by_id()["E1"].fields["hierarchy_scope"], "eli")
        self.assertIn("Set a company direction from multi-year evidence.", text)
        self.assertIn("Translate the company direction into a team plan.", text)
        self.assertIn("Use the team's source analysis as instruction.", text)

        missing_manager_edge = json.loads(json.dumps(plan))
        missing_manager_edge["steps"][2]["depends_on"] = []
        with self.assertRaisesRegex(ValueError, "parent-scope step"):
            bridge.render_taskflow(missing_manager_edge, conversation_id="root-chat", assistant_sha256="e" * 64)
        wrong_employee = json.loads(json.dumps(plan))
        wrong_employee["steps"][2]["owner"] = "Other Employee"
        with self.assertRaisesRegex(ValueError, "named employee"):
            bridge.render_taskflow(wrong_employee, conversation_id="root-chat", assistant_sha256="e" * 64)
        circular = json.loads(json.dumps(plan))
        circular["steps"][0]["depends_on"] = ["E1"]
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            bridge.render_taskflow(circular, conversation_id="root-chat", assistant_sha256="e" * 64)

    def test_root_successor_waits_for_effects_then_queues_once_with_outcome_zip(self):
        controller = bridge._load(bridge.CONTROLLER, "test_root_outcome_controller")
        cycle = bridge._load(bridge.HERE / "cognilode-root-memory-native-cycle",
                             "test_root_outcome_cycle")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            plan_path = root / "root.plan"
            plan_path.write_text("project Root Outcomes\nid root-outcomes\n[ ] A Deploy work\n"
                                 "    effect_probe_command: /usr/bin/python3 -I /tmp/probe.py outcome\n"
                                 "    effect_probe_expected: live\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "root.plan"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
            memory_sha = "a" * 64
            revision = "c" * 64
            effect_id = controller.stable_id("root-outcomes", revision, "A", "external_effect", "0")
            state = {"plan_path": str(plan_path), "root_job_id": "root-job",
                     "root_memory_packet_sha256": memory_sha}
            called = []
            def post(body):
                called.append(dict(body))
                if body["operation"] == "get_job":
                    if body["job_id"] == "root-job":
                        return {"ok": True, "job": {
                            "id": "root-job", "state": "complete", "provider": "chatgpt.com",
                            "conversation_id": "root-chat", "attachment_refs": [{
                                "ref": "file:/missing/root-memory.zip", "sha256": memory_sha,
                                "mirrors": ["s3://private/root-memory.zip"], "name": "root-memory.zip"}]}}
                    if body["job_id"] == effect_id:
                        return {"ok": True, "job": {"id": body["job_id"],
                                "state": "complete", "provider": "codex.external-effect",
                                "effect_evidence": [{"kind": "external_effect", "ref": "deployed"}]}}
                    if body["job_id"] in saved:
                        return {"ok": True, "job": saved[body["job_id"]]}
                    return {"error": "job_not_found"}
                if body["operation"] == "read":
                    return {"ok": True, "conversation": {"conversation_id": "root-chat",
                            "events": [{"role": "assistant", "content": "Root prior plan and report",
                                        "source_content_complete": True}]}}
                if body["operation"] == "enqueue_job":
                    saved[body["job_id"]] = dict(body)
                    return {"ok": True, "job": saved[body["job_id"]]}
                raise AssertionError(body)
            saved = {}
            waiting = bridge.enqueue_outcome_gated_successor(
                post, state=state, output_root=root, controller=controller, cycle=cycle,
                publish=lambda row: "s3://private/" + row["sha256"] + ".zip")
            self.assertEqual(waiting["reason"], "awaiting_verified_outcomes")
            self.assertFalse(any(row["operation"] == "enqueue_job" for row in called))

            plan_path.write_text("project Root Outcomes\nid root-outcomes\n"
                                 f"completed_effect A {revision} {effect_id}\n"
                                 "[x] A Deploy work\n"
                                 "    effect_probe_command: /usr/bin/python3 -I /tmp/probe.py outcome\n"
                                 "    effect_probe_expected: live\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "root.plan"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "completed"], check=True)
            verified = []
            with mock.patch.object(controller, "verify_completed_effects",
                                   side_effect=lambda queue, plan, step_ids: verified.append(step_ids)):
                result = bridge.enqueue_outcome_gated_successor(
                    post, state=state, output_root=root, controller=controller, cycle=cycle,
                    publish=lambda row: "s3://private/" + row["sha256"] + ".zip")
                again = bridge.enqueue_outcome_gated_successor(
                    post, state=state, output_root=root, controller=controller, cycle=cycle,
                    publish=lambda row: "s3://private/" + row["sha256"] + ".zip")
            self.assertEqual(verified, [{"A"}, {"A"}])
            self.assertEqual(result["phase"], "await_root")
            self.assertEqual(result["root_job_id"], again["root_job_id"])
            self.assertEqual(sum(row["operation"] == "enqueue_job" for row in called), 1)
            successor = saved[result["root_job_id"]]
            self.assertEqual(successor["provider"], "chatgpt.com")
            self.assertEqual(successor["reasoning_effort"], "xhigh")
            self.assertEqual(len(successor["attachment_refs"]), 2)
            outcome_zip = Path(successor["attachment_refs"][1]["ref"].removeprefix("file:"))
            with zipfile.ZipFile(outcome_zip) as archive:
                payload = json.loads(archive.read("OUTCOMES.json"))
                self.assertEqual(payload["completed_outcomes"][0]["effect_job_id"], effect_id)
                self.assertEqual(payload["prior_root_conversation"]["conversation_id"], "root-chat")

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
            source = subprocess.run(["git", "-C", directory, "show", "HEAD:" + installed[0][0].name],
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

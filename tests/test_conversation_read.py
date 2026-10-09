import importlib.machinery
import importlib.util
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-conversation-read"
loader = importlib.machinery.SourceFileLoader("cognilode_conversation_read_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
reader = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = reader
loader.exec_module(reader)


class ConversationDeltaTests(unittest.TestCase):
    def setUp(self):
        self.full = {"provider": "chatgpt.com", "conversation_id": "conversation-1",
                     "source": "hosted_conversation_service", "events": [
                         {"id": "u1", "role": "user", "text": "first", "occurred_at_utc": ""},
                         {"id": "a1", "role": "assistant", "text": "reply", "occurred_at_utc": ""},
                     ]}

    def test_second_read_only_returns_new_turn(self):
        first = reader.delta_view(self.full)
        self.assertEqual(first["new_events"], 2)
        extended = {**self.full, "events": self.full["events"] + [
            {"id": "u2", "role": "user", "text": "next", "occurred_at_utc": ""}]}
        second = reader.delta_view(extended, first["next_cursor"])
        self.assertEqual(second["new_events"], 1)
        self.assertEqual(second["events"][0]["text"], "next")
        self.assertEqual(reader.delta_view(extended, second["next_cursor"])["new_events"], 0)

    def test_changed_prefix_refuses_silent_delta(self):
        cursor = reader.delta_view(self.full)["next_cursor"]
        changed = {**self.full, "events": [{**self.full["events"][0], "text": "corrected"},
                                            self.full["events"][1]]}
        with self.assertRaisesRegex(ValueError, "refresh full conversation"):
            reader.delta_view(changed, cursor)

    def test_cursor_is_conversation_bound(self):
        cursor = reader.delta_view(self.full)["next_cursor"]
        with self.assertRaisesRegex(ValueError, "another conversation"):
            reader.delta_view({**self.full, "conversation_id": "conversation-2"}, cursor)

    def test_remembered_reader_sees_full_once_then_only_new_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            source={**self.full}
            def invoke(reader_id="employee-a"):
                argv=["conversation-read", "--provider", "chatgpt.com", "--conversation-id",
                      "conversation-1", "--remember", "--reader-id", reader_id,
                      "--cursor-root", temporary, "--remember-store", "local"]
                output=io.StringIO()
                with mock.patch.object(sys,"argv",argv), mock.patch.object(reader,"read_conversation",return_value=source), \
                     redirect_stdout(output):
                    self.assertEqual(reader.main(),0)
                return json.loads(output.getvalue())
            self.assertEqual(invoke()["new_events"],2)
            self.assertEqual(invoke()["new_events"],0)
            source={**self.full,"events":self.full["events"] + [
                {"id":"u2","role":"user","text":"next","occurred_at_utc":""}]}
            self.assertEqual([r["text"] for r in invoke()["events"]],["next"])
            self.assertEqual(invoke("employee-b")["new_events"],3)
            source={**source,"events":[{**source["events"][0],"text":"corrected"},
                                        *source["events"][1:]]}
            refreshed=invoke()
            self.assertEqual(refreshed["cursor_reset_reason"],"source_prefix_changed")
            self.assertEqual(refreshed["new_events"],3)

    def test_shared_remember_follows_employee_across_devices_with_cas(self):
        state={"cursor":"", "generation":0}
        source={**self.full}
        sender=mock.Mock()
        def post(body):
            identity={key:body[key] for key in ("reader_id","provider","conversation_id")}
            if body["operation"] == "get_reader_cursor":
                return {"ok":True, **identity, **state}
            if body["operation"] == "advance_reader_cursor":
                if body["expected_generation"] != state["generation"]:
                    return {"ok":True, **identity, **state, "advanced":False, "conflict":True}
                state["generation"]+=1
                state["cursor"]=body["next_cursor"]
                return {"ok":True, **identity, **state, "advanced":True, "conflict":False}
            raise AssertionError(body)
        sender.operator_memory_post.side_effect=post
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            def invoke(device):
                output=io.StringIO()
                argv=["conversation-read","--provider","chatgpt.com","--conversation-id","conversation-1",
                      "--remember","--reader-id","employee-a","--cursor-root",device]
                with mock.patch.object(sys,"argv",argv), mock.patch.object(reader,"_load_script",return_value=sender), \
                     mock.patch.object(reader,"read_conversation",return_value=source),redirect_stdout(output):
                    self.assertEqual(reader.main(),0)
                return json.loads(output.getvalue())
            self.assertEqual(invoke(one)["new_events"],2)
            self.assertEqual(invoke(two)["new_events"],0)
            source={**self.full,"events":self.full["events"] + [
                {"id":"u2","role":"user","text":"next","occurred_at_utc":""}]}
            self.assertEqual([event["text"] for event in invoke(two)["events"]],["next"])
            self.assertEqual(invoke(one)["new_events"],0)
            self.assertEqual(state["generation"],4)
            self.assertEqual(list(Path(one).iterdir()),[])
            self.assertEqual(list(Path(two).iterdir()),[])

    def test_job_id_resolves_only_matching_central_provider_readback(self):
        job={"id":"job-1","state":"complete","provider":"chatgpt.com",
             "conversation_id":"conversation-1","effect_evidence":[
                 {"kind":"provider_conversation","ref":"conversation-1"},
                 {"kind":"central_conversation_readback","ref":"conversation-1"}]}
        sender=mock.Mock()
        sender.operator_memory_post.return_value={"ok":True,"job":job}
        with mock.patch.object(reader,"_load_script",return_value=sender):
            link=reader.resolve_job_conversation("job-1")
            self.assertTrue(link["conversation_ready"])
            self.assertEqual(link["conversation_id"],"conversation-1")
            sender.operator_memory_post.assert_called_once_with({"operation":"get_job","job_id":"job-1"})
            with self.assertRaisesRegex(ValueError,"identity mismatch"):
                reader.resolve_job_conversation("job-1",expected_provider="gemini.com")
            job["effect_evidence"].pop()
            with self.assertRaisesRegex(ValueError,"central provider readback"):
                reader.resolve_job_conversation("job-1")

    def test_job_id_returns_pending_state_without_claiming_provider_response(self):
        sender=mock.Mock()
        sender.operator_memory_post.return_value={"ok":True,"job":{
            "id":"job-1","state":"queued","provider":"chatgpt.com"}}
        output=io.StringIO()
        with mock.patch.object(sys,"argv",["conversation-read","--job-id","job-1"]), \
             mock.patch.object(reader,"_load_script",return_value=sender), \
             mock.patch.object(reader,"read_conversation") as read, redirect_stdout(output):
            self.assertEqual(reader.main(),0)
        self.assertEqual(json.loads(output.getvalue())["conversation_ready"],False)
        read.assert_not_called()

    def _codex_taskflow_job(self, state="complete"):
        cid = "b0f26e7e-5a33-4ec6-a815-df7c70f20942"
        return {"id":"tf-research-1", "state":state, "provider":"codex.research",
                "effect_evidence":[{"kind":"codex_session_identity", "job_id":"tf-research-1",
                                    "ref":cid, "conversation_id":cid,
                                    "conversation_id_source":"codex_cli_stderr_header",
                                    "run_id":"secretary-run-1", "run_dir":"/run/secretary-run-1",
                                    "compact_receipt_sha256":"a"*64, "codex_stderr_sha256":"b"*64,
                                    "central_readback_verified":False}]}

    def _codex_central(self, cid, **capture_overrides):
        capture = {"source_kind":"codex_rollout", "source_sha256":"c"*64,
                   "source_response_complete":True, "source_complete":True,
                   "drive_verified":True, "drive_source_sha256":"c"*64,
                   "drive_object_sha256":"d"*64,
                   "drive_verified_at":"2026-10-09T15:00:00Z", "drive_conversation_id":cid}
        capture.update(capture_overrides)
        return {"ok":True, "conversation":{"provider":"openai-codex",
                 "conversation_id":cid, "capture":capture, "events":[]}}

    def test_taskflow_codex_job_never_reads_central_before_completion(self):
        for state in ("queued", "effect_pending"):
            with self.subTest(state=state):
                job = self._codex_taskflow_job(state)
                sender = mock.Mock()
                sender.operator_memory_post.return_value = {"ok":True, "job":job}
                with mock.patch.object(reader, "_load_script", return_value=sender):
                    link = reader.resolve_job_conversation(job["id"], expected_provider="codex-d1")
                self.assertFalse(link["conversation_ready"])
                self.assertEqual(link["conversation_id"], job["effect_evidence"][0]["conversation_id"])
                self.assertEqual(link["job_provider"], "codex.research")
                sender.operator_memory_post.assert_called_once_with(
                    {"operation":"get_job", "job_id":job["id"]})

    def test_taskflow_codex_job_requires_complete_exact_drive_join(self):
        job = self._codex_taskflow_job()
        cid = job["effect_evidence"][0]["conversation_id"]
        cases = [
            ("not admitted", {"ok":True}, False),
            ("response incomplete", self._codex_central(cid, source_response_complete=False), False),
            ("active prefix", self._codex_central(cid, source_complete=False), False),
            ("drive not verified", self._codex_central(cid, drive_verified=False), False),
            ("source mismatch", self._codex_central(cid, drive_source_sha256="e"*64), False),
            ("wrong drive session", self._codex_central(cid, drive_conversation_id="other"), False),
            ("verified", self._codex_central(cid), True),
        ]
        for label, central, expected in cases:
            with self.subTest(label=label):
                sender = mock.Mock()
                sender.operator_memory_post.side_effect = [{"ok":True, "job":job}, central]
                with mock.patch.object(reader, "_load_script", return_value=sender):
                    link = reader.resolve_job_conversation(job["id"])
                self.assertEqual(link["conversation_ready"], expected)
                self.assertEqual(link["conversation_id"], cid)
                self.assertEqual(link["provider"], "codex-d1")
                self.assertEqual(sender.operator_memory_post.call_args.args[0],
                                 {"operation":"read", "provider":"openai-codex", "conversation_id":cid})

    def test_taskflow_codex_admission_lag_keeps_session_link(self):
        job = self._codex_taskflow_job()
        cid = job["effect_evidence"][0]["conversation_id"]
        sender = mock.Mock()
        sender.operator_memory_post.side_effect = [
            {"ok":True, "job":job}, RuntimeError("central read temporarily unavailable")]
        with mock.patch.object(reader, "_load_script", return_value=sender):
            link = reader.resolve_job_conversation(job["id"])
        self.assertFalse(link["conversation_ready"])
        self.assertEqual(link["conversation_id"], cid)
        job["effect_evidence"] = []
        sender.operator_memory_post.side_effect = None
        sender.operator_memory_post.return_value = {"ok":True, "job":job}
        with mock.patch.object(reader, "_load_script", return_value=sender):
            missing = reader.resolve_job_conversation(job["id"])
        self.assertFalse(missing["conversation_ready"])
        self.assertIsNone(missing["conversation_id"])

    def test_taskflow_codex_job_fails_closed_on_forged_identity_and_central_mismatch(self):
        job = self._codex_taskflow_job()
        cid = job["effect_evidence"][0]["conversation_id"]
        sender = mock.Mock()
        with mock.patch.object(reader, "_load_script", return_value=sender):
            for key, forged in (("job_id", "another-job"), ("conversation_id_source", "echoed_text"),
                                ("codex_stderr_sha256", "bad")):
                with self.subTest(key=key):
                    bad = json.loads(json.dumps(job))
                    bad["effect_evidence"][0][key] = forged
                    sender.operator_memory_post.return_value = {"ok":True, "job":bad}
                    with self.assertRaisesRegex(ValueError, "session identity evidence mismatch"):
                        reader.resolve_job_conversation(job["id"])
            sender.operator_memory_post.side_effect = [
                {"ok":True, "job":job},
                {**self._codex_central(cid), "conversation":{
                    **self._codex_central(cid)["conversation"], "conversation_id":"another-session"}},
            ]
            with self.assertRaisesRegex(ValueError, "central Codex conversation identity mismatch"):
                reader.resolve_job_conversation(job["id"])

    def test_taskflow_codex_remember_does_not_advance_until_drive_verified(self):
        job = self._codex_taskflow_job()
        cid = job["effect_evidence"][0]["conversation_id"]
        central = self._codex_central(cid, drive_verified=False)
        sender = mock.Mock()
        def post(body):
            return {"ok":True, "job":job} if body["operation"] == "get_job" else central
        sender.operator_memory_post.side_effect = post
        with tempfile.TemporaryDirectory() as temporary:
            argv = ["conversation-read", "--job-id", job["id"], "--remember",
                    "--cursor-root", temporary, "--remember-store", "local"]
            full = {"provider":"openai-codex", "conversation_id":cid,
                    "source":"hosted_d1_codex_drive_verified", "events":[
                        {"id":"a1", "role":"assistant", "text":"done", "occurred_at_utc":""}],
                    "coverage":"admitted_codex_rollout_messages_drive_verified",
                    "all_provider_events_known":False}
            def invoke():
                output = io.StringIO()
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.object(reader, "_load_script", return_value=sender), \
                     mock.patch.object(reader, "read_conversation", return_value=full) as read, \
                     redirect_stdout(output):
                    self.assertEqual(reader.main(), 0)
                return json.loads(output.getvalue()), read.call_count
            first, calls = invoke()
            self.assertFalse(first["conversation_ready"])
            self.assertEqual(first["conversation_id"], cid)
            self.assertEqual(calls, 0)
            self.assertEqual(list(Path(temporary).rglob("*")), [])
            central = self._codex_central(cid)
            second, calls = invoke()
            self.assertEqual(calls, 1)
            self.assertTrue(second["queue_job"]["conversation_ready"])
            self.assertEqual(second["new_events"], 1)
            third, _ = invoke()
            self.assertEqual(third["new_events"], 0)

    def test_hosted_provider_read_preserves_provider_identity(self):
        response = {"conversation": {"provider": "gemini.com", "conversation_id": "g-1", "events": [
            {"id": "u-1", "role": "user", "content": "question", "source_content_complete": True},
            {"id": "a-1", "role": "assistant", "content": "answer", "source_content_complete": True},
        ]}}
        sender = mock.Mock()
        sender.operator_memory_post.return_value = response
        with mock.patch.object(reader, "_load_script", return_value=sender):
            full = reader.read_conversation("gemini.com", "g-1")
            self.assertEqual(full["provider"], "gemini.com")
            self.assertEqual(full["events"][1]["text"], "answer")
            self.assertEqual(sender.operator_memory_post.call_args.args[0]["provider"], "gemini.com")
            sender.operator_memory_post.return_value = {"conversation": {**response["conversation"], "provider": "chatgpt.com"}}
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                reader.read_conversation("gemini.com", "g-1")

    def test_codex_d1_interim_is_explicitly_drive_unverified(self):
        conversation = {"provider": "openai-codex", "conversation_id": "c-1",
                        "capture": {"source_kind": "codex_rollout", "source_sha256": "a" * 64,
                                    "drive_verified": False, "source_event_count": 1,
                                    "source_full_message_count": 1},
                        "events": [{"id": "event-1", "index": 0, "role": "user", "content": "work"}]}
        sender = mock.Mock()
        sender.operator_memory_post.return_value = {"conversation": conversation}
        with mock.patch.object(reader, "_load_script", return_value=sender):
            result = reader.read_conversation("codex-d1", "c-1")
            self.assertEqual(result["provider"], "openai-codex")
            self.assertEqual(result["coverage"], "admitted_codex_rollout_legacy_unsplit_drive_unverified")
            self.assertFalse(result["all_provider_events_known"])
            self.assertEqual(result["source_receipt"]["source_sha256"], "a" * 64)
            self.assertEqual(sender.operator_memory_post.call_args.args[0]["provider"], "openai-codex")
            sender.operator_memory_post.return_value = {"conversation": {**conversation, "capture": {
                **conversation["capture"], "drive_verified": True}}}
            with self.assertRaisesRegex(ValueError, "source receipt"):
                reader.read_conversation("codex-d1", "c-1")

    def test_codex_d1_verified_read_requires_exact_source_join(self):
        cid = "b0f26e7e-5a33-4ec6-a815-df7c70f20942"
        conversation = self._codex_central(cid)["conversation"]
        conversation["capture"].update(source_event_count=1, source_full_message_count=1)
        conversation["events"] = [{"id":"event-1", "index":0, "role":"assistant", "content":"done"}]
        sender = mock.Mock()
        sender.operator_memory_post.return_value = {"conversation":conversation}
        with mock.patch.object(reader, "_load_script", return_value=sender):
            full = reader.read_conversation("codex-d1", cid)
            self.assertEqual(full["source"], "hosted_d1_codex_drive_verified")
            self.assertEqual(full["coverage"], "admitted_codex_rollout_legacy_unsplit_drive_verified")
            self.assertEqual(full["events"][0]["text"], "done")
            conversation["capture"]["drive_source_sha256"] = "e" * 64
            with self.assertRaisesRegex(ValueError, "Drive join is incomplete"):
                reader.read_conversation("codex-d1", cid)

    def test_codex_d1_reassembles_and_verifies_long_original_message(self):
        response = "😀" * 110_000
        original = [("user", "request", 7), ("assistant", response, 8)]
        digest_rows = [[role, text, hashlib.sha256(text.encode()).hexdigest(), ordinal]
                       for role, text, ordinal in original]
        full_digest = hashlib.sha256(json.dumps(digest_rows, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        capture = {"source_kind": "codex_rollout", "source_sha256": "b" * 64,
                   "drive_verified": False, "source_event_count": 3,
                   "source_full_message_count": 2,
                   "source_full_messages_sha256": full_digest,
                   "source_has_segmented_messages": True,
                   "source_message_segments": [
                       {"role": "user", "source_ordinal": 7, "content_sha256": digest_rows[0][2],
                        "segment_start": 0, "segment_count": 1, "segment_source_ids": ["u"]},
                       {"role": "assistant", "source_ordinal": 8, "content_sha256": digest_rows[1][2],
                        "segment_start": 1, "segment_count": 2, "segment_source_ids": ["a1", "a2"]},
                   ]}
        events = [{"index": 0, "role": "user", "content": "request"},
                  {"index": 1, "role": "assistant", "content": response[:80_000]},
                  {"index": 2, "role": "assistant", "content": response[80_000:]}]
        sender = mock.Mock()
        sender.operator_memory_post.return_value = {"conversation": {
            "provider": "openai-codex", "conversation_id": "c-long", "capture": capture, "events": events}}
        with mock.patch.object(reader, "_load_script", return_value=sender):
            result = reader.read_conversation("codex-d1", "c-long")
            self.assertEqual(len(result["events"]), 2)
            self.assertEqual(result["events"][1]["text"], response)
            self.assertEqual(result["events"][1]["source_segment_ids"], ["a1", "a2"])
            self.assertEqual(result["coverage"], "admitted_codex_rollout_messages_drive_unverified")
            events[2] = {**events[2], "content": response[80_000:-1] + "x"}
            with self.assertRaisesRegex(ValueError, "message SHA-256 differs"):
                reader.read_conversation("codex-d1", "c-long")

    def test_deployed_reader_can_import_sibling_and_checkout_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            scripts = checkout / "deploy" / "conversation-vacuum"
            package = checkout / "src" / "memory_stock"
            scripts.mkdir(parents=True)
            package.mkdir(parents=True)
            (scripts / "drive_transport_errors.py").write_text("STATUS = 'classified'\n")
            (package / "__init__.py").write_text("")
            (package / "unified_conversation.py").write_text("STATUS = 'reconciled'\n")
            deployed = scripts / "canonical_full_readback.py"
            deployed.write_text(
                "from drive_transport_errors import STATUS as TRANSPORT\n"
                "def read_full_conversation(*args):\n"
                "    from memory_stock.unified_conversation import STATUS as MEMORY\n"
                "    return (TRANSPORT, MEMORY)\n"
            )
            check = subprocess.run(
                [sys.executable, "-c", (
                    "import importlib.machinery, importlib.util, sys; "
                    "source=importlib.machinery.SourceFileLoader('reader',sys.argv[1]); "
                    "spec=importlib.util.spec_from_loader(source.name,source); "
                    "module=importlib.util.module_from_spec(spec); "
                    "sys.modules[source.name]=module; source.exec_module(module); "
                    "loaded=module._load_script(__import__('pathlib').Path(sys.argv[2]),'deployed'); "
                    "print('/'.join(loaded.read_full_conversation()))"
                ), str(SCRIPT), str(deployed)],
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(check.stdout.strip(), "classified/reconciled")

    def test_historical_stock_federates_verified_events_and_rejects_changed_source_hash(self):
        rendered = "# Example\nConversation: historical-id\n"
        source = {
            "schema": "memory_stock.historical_s3_delta_read.v1",
            "changed": True, "reset_required": False,
            "provider": "historical-s3", "conversation_id": "historical-id",
            "rendered_markdown": rendered,
            "rendered_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "message_count": 2,
            "coverage": "rendered_user_assistant_source_with_metadata_checks",
            "source_receipt": {"s3_key": "conversations/historical-id.md.gz"},
            "events": [
                {"node_id": "u1", "role": "user", "text": "plan", "format_and_time": "text · 2024-01-01T00:00:00Z"},
                {"node_id": "a1", "parent_node_id": "u1", "role": "assistant", "text": "work", "format_and_time": "text · 2024-01-01T00:01:00Z"},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "reader.py"
            script.write_text("# mocked reader\n")
            with mock.patch.object(reader.subprocess, "run", return_value=mock.Mock(stdout=json.dumps(source))) as run:
                full = reader.read_conversation("historical-s3", "historical-id", historical_reader=script)
                self.assertEqual(full["events"][1]["id"], "a1")
                self.assertEqual(full["events"][1]["parent_node_id"], "u1")
                self.assertEqual(reader.delta_view(full, reader.delta_view(full)["next_cursor"])["new_events"], 0)
                self.assertEqual(run.call_args.args[0][2:], ["read", "historical-id", "--json"])
            source["rendered_markdown"] += "tampered"
            with mock.patch.object(reader.subprocess, "run", return_value=mock.Mock(stdout=json.dumps(source))):
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    reader.read_conversation("historical-s3", "historical-id", historical_reader=script)


if __name__ == "__main__":
    unittest.main()

import importlib.machinery
import importlib.util
import hashlib
import json
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

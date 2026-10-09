import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest


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


if __name__ == "__main__":
    unittest.main()

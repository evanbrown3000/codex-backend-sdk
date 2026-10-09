import importlib.machinery
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-conversation-search"
loader = importlib.machinery.SourceFileLoader("cognilode_conversation_search_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
searcher = importlib.util.module_from_spec(spec)
loader.exec_module(searcher)


class ConversationSearchTests(unittest.TestCase):
    def test_d1_hits_expose_cross_provider_reader_coordinates_without_claiming_custody(self):
        hits = [{"conversation_key": "openai-codex:01a10ad5-a266-7010-9dd2-e7c7cfc267f7",
                 "excerpt": "research"},
                {"conversation_key": "chatgpt-export-format:export-source:abc",
                 "excerpt": "planning"},
                {"conversation_key": "broken", "excerpt": "unknown"}]
        result = searcher._with_read_coordinates(hits, "conversation_key")
        self.assertEqual(result[0]["read_args"],
                         {"provider": "openai-codex",
                          "conversation_id": "01a10ad5-a266-7010-9dd2-e7c7cfc267f7"})
        self.assertEqual(result[1]["conversation_id"], "export-source:abc")
        self.assertNotIn("read_args", result[2])
        self.assertEqual(hits[0], {"conversation_key": hits[0]["conversation_key"],
                                   "excerpt": "research"})

    def test_sources_keep_separate_scopes_and_survive_one_transport_failure(self):
        with mock.patch.object(searcher, "hosted_search", side_effect=ConnectionError("quota")), \
             mock.patch.object(searcher, "historical_search", return_value={
                 "scope": "title_or_conversation_id_only", "rows": [{"conversation_id": "c-1"}]}):
            result = searcher.search("DecisionX", 10, "all", Path("unused"))
        self.assertTrue(result["at_least_one_source_succeeded"])
        self.assertEqual(result["groups"]["hosted"], {"ok": False, "error": "ConnectionError"})
        self.assertEqual(result["groups"]["historical_s3"]["rows"][0]["conversation_id"], "c-1")
        self.assertEqual(result["groups"]["historical_s3"]["scope"], "title_or_conversation_id_only")

    def test_source_selection_does_not_call_other_transport(self):
        with mock.patch.object(searcher, "hosted_search", return_value={
            "scope": "hosted_d1_segments_and_turns", "segments": [], "turns": []}) as hosted, \
             mock.patch.object(searcher, "historical_search", side_effect=AssertionError("unexpected")):
            result = searcher.search("RPE", 2, "hosted", Path("unused"))
        self.assertEqual(list(result["groups"]), ["hosted"])
        hosted.assert_called_once_with("RPE", 2)


if __name__ == "__main__":
    unittest.main()

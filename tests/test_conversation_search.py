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

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import json
import sqlite3


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-chatmode-export-retirement"
loader = SourceFileLoader("chatmode_export_retirement_test", str(SCRIPT))
spec = spec_from_loader(loader.name, loader)
retirement = module_from_spec(spec)
loader.exec_module(retirement)


def test_text_projection_requires_exact_drive_readback_and_keeps_original_url(monkeypatch):
    cid = "original@export-version"
    original = "original"
    events = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}]
    remote = {"provider": "chatgpt-export-format", "conversation_id": cid,
              "conversation_urls": ["https://chatgpt.com/c/original"],
              "updated_at": "2026-10-09T00:00:00Z", "prompt_sha256": "p", "response_sha256": "r",
              "events": events,
              "capture": {"source_conversation_id": original,
                          "source_conversation_sha256": "source-sha", "export_etag": '"etag"',
                          "source_complete": False, "source_text_projection_complete": True,
                          "normalized_events_sha256": "normalized-sha"}}
    row = {"archive_etag": '"etag"', "conversation_id": original,
           "stored_conversation_id": cid, "source_conversation_sha256": "source-sha",
           "normalized_events_sha256": "normalized-sha"}
    monkeypatch.setattr(retirement.sender, "operator_memory_post", lambda _: {"conversation": remote})
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE d1_drive_admissions(provider TEXT,conversation_id TEXT,
        source_revision TEXT,event_sha256 TEXT,event_count INTEGER,source_stream_id TEXT,
        verified_at TEXT)""")
    assert retirement.verify_one(row, '"etag"', db) is False
    event_sha = retirement.digest(events)
    revision = retirement.digest([remote["provider"], cid, remote["updated_at"], "p", "r"])
    db.execute("INSERT INTO d1_drive_admissions VALUES(?,?,?,?,?,?,?)",
               (remote["provider"], cid, revision, event_sha, 2,
                "d1:chatgpt-export-format:" + cid + ":" + event_sha[:24], "now"))
    assert retirement.verify_one(row, '"etag"', db) is True
    remote["conversation_urls"] = ["https://chatgpt.com/c/other"]
    try:
        retirement.verify_one(row, '"etag"', db)
    except RuntimeError as exc:
        assert "identity/fidelity" in str(exc)
    else:
        raise AssertionError("lost original provider URL should reject retirement proof")


def test_skipped_thought_only_source_is_audited_without_retiring_zip(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "cursor.json").write_text(json.dumps({"archive_etag": '"etag"',
        "next_shard": -1, "deferred": [], "admitted": 0, "exact_deduped": 0,
        "source_skipped_conversations": 1}))
    (root / "d1_exact_receipts.jsonl").write_text("")
    skipped = {"archive_etag": '"etag"', "member": "conversations-145.json",
               "offset": 44, "source_item_sha256": "sha", "source_complete": False,
               "classification": "conversation_without_visible_text_skipped"}
    (root / "skipped_source_conversations.jsonl").write_text(json.dumps(skipped) + "\n")
    db_path = tmp_path / "writer.sqlite3"
    sqlite3.connect(db_path).close()
    result = retirement.run(root, db_path, 1)
    assert result["source_retirement_allowed"] is False
    assert result["verified"] == 0
    skipped["source_complete"] = True
    (root / "skipped_source_conversations.jsonl").write_text(json.dumps(skipped) + "\n")
    try:
        retirement.run(root, db_path, 1)
        assert False, "falsely complete skipped source must reject"
    except RuntimeError as exc:
        assert "skipped source" in str(exc)

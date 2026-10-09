from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
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

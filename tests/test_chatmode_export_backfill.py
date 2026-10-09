from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import json
import os
import sqlite3
import zipfile
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-chatmode-export-backfill"
loader = SourceFileLoader("chatmode_export_backfill_test", str(SCRIPT))
spec = spec_from_loader(loader.name, loader)
backfill = module_from_spec(spec)
loader.exec_module(backfill)


def test_text_projection_preserves_branch_edges_and_nontext_omission():
    source = {"mapping": {
        "root": {"parent": None, "message": None},
        "u": {"parent": "root", "message": {"id": "user-1", "author": {"role": "user"},
            "content": {"parts": ["Ask"]}, "create_time": 1}},
        "a": {"parent": "u", "message": {"id": "assistant-1", "author": {"role": "assistant"},
            "content": {"parts": ["Answer"]}, "create_time": 2}},
        "alternate": {"parent": "u", "message": {"id": "assistant-2", "author": {"role": "assistant"},
            "content": {"parts": ["Other branch"]}, "create_time": 3}},
        "media": {"parent": "a", "message": {"id": "media-1", "author": {"role": "user"},
            "content": {"content_type": "image_asset_pointer", "parts": []}, "create_time": 4}},
    }}
    events, summary = backfill.normalize(source)
    assert [e["content"] for e in events] == ["Ask", "Answer", "Other branch"]
    assert summary["source_nodes"] == 5
    assert summary["source_nontext_messages"] == 1
    assert ("alternate", "u") in summary["branch_parent_edges"]
    assert ("media", "a") in summary["branch_parent_edges"]


def test_thought_nodes_are_omitted_and_hashed_not_rendered_as_replies():
    source = {"mapping": {
        "u": {"parent": None, "message": {"id": "u", "author": {"role": "user"},
            "content": {"parts": ["Visible question"]}}},
        "t": {"parent": "u", "message": {"id": "t", "author": {"role": "assistant"},
            "content": {"content_type": "thoughts", "thoughts": [
                {"summary": "Private summary", "content": "Private internal thought"}]}}},
    }}
    events, summary = backfill.normalize(source)
    assert [e["content"] for e in events] == ["Visible question"]
    assert summary["source_omitted_thought_count"] == 1
    assert len(summary["source_omitted_thought_sha256"]) == 64
    assert "Private" not in json.dumps(summary)


def test_thought_only_source_gets_auditable_skip_receipt_without_d1(monkeypatch, tmp_path):
    source = {"id": "6ab2d801-82d4-83ea-a48e-d7cb5afd1846", "mapping": {
        "t": {"parent": None, "message": {"id": "t", "author": {"role": "assistant"},
            "content": {"content_type": "thoughts", "thoughts": [
                {"summary": "Not a final answer", "content": "Private internal thought"}]}}}}}
    info = SimpleNamespace(filename="conversations-145.json", CRC=123)
    archive_path = tmp_path / "one.zip"
    import zipfile
    with zipfile.ZipFile(archive_path, "w") as out:
        out.writestr(info.filename, json.dumps([source]))
    source_handle = SimpleNamespace(etag='"etag"', size=archive_path.stat().st_size)
    state = {"deferred": [{"shard": info.filename, "offset": 0,
                           "reason": "conversation has no bounded textual events"}],
             "source_skipped_conversations": 0}
    state_path = tmp_path / "cursor.json"
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr(backfill, "prior_conversation", lambda cid: (_ for _ in ()).throw(
        AssertionError("thought-only item must not call D1")))
    with zipfile.ZipFile(archive_path) as archive:
        backfill.repair_one_deferred(archive, source_handle, tmp_path, state_path, state)
    rows = [json.loads(x) for x in (tmp_path / "skipped_source_conversations.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["source_conversation_id"] == source["id"]
    assert rows[0]["omitted_thought_count"] == 1
    assert rows[0]["source_complete"] is False
    assert "Private" not in json.dumps(rows)
    assert state["deferred"] == [] and state["source_skipped_conversations"] == 1


def test_old_source_collision_gets_versioned_id(monkeypatch):
    old = {"capture": {"source_conversation_sha256": "different", "export_etag": '"old"'}}
    monkeypatch.setattr(backfill, "prior_conversation", lambda cid: old if cid == "original" else None)
    identity, prior = backfill.store_identity("original", "current", SimpleNamespace(etag='"new"'))
    assert identity.startswith("original@export-")
    assert prior is None


def test_contentful_idless_source_has_synthetic_identity_and_no_provider_uri(monkeypatch):
    monkeypatch.setattr(backfill, "prior_conversation", lambda _: None)
    source = SimpleNamespace(etag='"snapshot"')
    info = SimpleNamespace(filename="conversations-148.json")
    conversation = {"mapping": {"u": {"message": {"author": {"role": "user"},
                                      "content": {"parts": ["text"]}}}}}
    cid, original, prior = backfill.source_identity(conversation, info, source, 29)
    assert cid.startswith("export-source:")
    assert original is None and prior is None
    assert backfill.source_identity(conversation, info, source, 29)[0] == cid


def test_empty_export_item_is_audited_without_conversation(tmp_path):
    info = SimpleNamespace(filename="conversations-148.json", CRC=123)
    source = SimpleNamespace(etag='"snapshot"', size=10)
    backfill.append_empty_source_receipt(tmp_path, info, source, 29)
    backfill.append_empty_source_receipt(tmp_path, info, source, 29)
    rows = (tmp_path / "nonconversation_source_items.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["classification"] == "empty_nonconversation_json_object"


def test_same_source_id_is_idempotent(monkeypatch):
    expected = {"capture": {"source_conversation_sha256": "current", "export_etag": '"new"'}}
    monkeypatch.setattr(backfill, "prior_conversation", lambda cid: expected)
    assert backfill.store_identity("original", "current", SimpleNamespace(etag='"new"')) == (
        "original", expected)


def test_drive_lag_counts_only_this_export_and_later_drive_proof(tmp_path):
    row = {"archive_etag": '"etag"', "stored_conversation_id": "cid",
           "d1_exact_verified_at": "2026-10-09T14:00:00+00:00"}
    (tmp_path / "d1_exact_receipts.jsonl").write_text(json.dumps(row) + "\n")
    db_path = tmp_path / "writer.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE d1_drive_admissions(provider TEXT,conversation_id TEXT,
            verified_at TEXT)""")
        db.execute("INSERT INTO d1_drive_admissions VALUES(?,?,?)",
                   ("chatgpt-export-format", "cid", "2026-10-09T13:00:00+00:00"))
    assert backfill.drive_lag(tmp_path, '"etag"', {"admitted": 1}, db_path)["pending_drive"] == 1
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO d1_drive_admissions VALUES(?,?,?)",
                   ("chatgpt-export-format", "cid", "2026-10-09T15:00:00+00:00"))
    assert backfill.drive_lag(tmp_path, '"etag"', {"admitted": 1}, db_path)["pending_drive"] == 0


def test_drive_lag_reads_legacy_receipt_without_stored_id(tmp_path):
    row = {"archive_etag": '"etag"', "conversation_id": "original",
           "d1_exact_verified_at": "2026-10-09T14:00:00+00:00"}
    (tmp_path / "d1_exact_receipts.jsonl").write_text(json.dumps(row) + "\n")
    db_path = tmp_path / "writer.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE d1_drive_admissions(provider TEXT,conversation_id TEXT,
            verified_at TEXT)""")
    assert backfill.drive_lag(tmp_path, '"etag"', {"admitted": 1}, db_path) == {
        "d1_receipts": 1, "drive_verified_after_d1": 0, "pending_drive": 1}


def test_high_water_pauses_before_remote_zip_request(monkeypatch, tmp_path):
    url_file = tmp_path / "url"
    url_file.write_text("https://chatgpt.com/backend-api/estuary/content?private\n")
    os.chmod(url_file, 0o600)
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "cursor.json").write_text(json.dumps({"archive_etag": '"etag"',
        "archive_bytes": 10, "admitted": 500, "exact_deduped": 0, "next_shard": 1,
        "position": 2, "deferred": []}))
    monkeypatch.setattr(backfill, "AuthenticatedRangeFile", lambda *_, **__: (_ for _ in ()).throw(
        AssertionError("remote ZIP must not be requested above high water")))
    result = backfill.run(url_file, maximum=100, state_root=state_root)
    assert result["state"] == "drive_lag_backpressure"
    assert result["pending_drive"] == 500
    assert json.loads((state_root / "cursor.json").read_text())["backpressure_paused"] is True


def test_expired_source_promotes_candidate_but_old_drive_lag_still_pauses(monkeypatch, tmp_path):
    url_file = tmp_path / "privacy-export-url.txt"
    candidate_file = tmp_path / "privacy-export-next-url.txt"
    old_url = "https://chatgpt.com/backend-api/estuary/content?old"
    new_url = "https://chatgpt.com/backend-api/estuary/content?new"
    url_file.write_text(old_url + "\n")
    candidate_file.write_text(new_url + "\n")
    os.chmod(url_file, 0o600)
    os.chmod(candidate_file, 0o600)
    state_root = tmp_path / "state"
    state_root.mkdir()
    old_state = {"archive_etag": '"old"', "archive_bytes": 123,
                 "admitted": 500, "exact_deduped": 0, "next_shard": 3,
                 "position": 6, "deferred": []}
    (state_root / "cursor.json").write_text(json.dumps(old_state))
    monkeypatch.setattr(backfill.sender, "new_session", lambda *a: object())
    monkeypatch.setattr(backfill, "verify_candidate_account", lambda *a: None)

    class Expired(Exception):
        response = SimpleNamespace(status_code=403)

    def source(session, url, **kwargs):
        if url == old_url:
            raise Expired()
        assert url == new_url
        return SimpleNamespace(etag='"new"', size=456)

    monkeypatch.setattr(backfill, "AuthenticatedRangeFile", source)
    result = backfill.run(url_file, maximum=100, state_root=state_root)
    assert result["state"] == "drive_lag_backpressure"
    assert result["archived_pending_drive"] == 500
    assert url_file.read_text().strip() == new_url
    assert not (state_root / "cursor.json").exists()
    archived = list((state_root / "superseded-snapshots").glob("*/gap-manifest.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text())["replacement_covers_old_gaps"] is None


def test_renewed_url_same_archive_keeps_exact_cursor(monkeypatch, tmp_path):
    url_file = tmp_path / "privacy-export-url.txt"
    candidate_file = tmp_path / "privacy-export-next-url.txt"
    old_url = "https://chatgpt.com/backend-api/estuary/content?old"
    new_url = "https://chatgpt.com/backend-api/estuary/content?new"
    url_file.write_text(old_url + "\n")
    candidate_file.write_text(new_url + "\n")
    os.chmod(url_file, 0o600)
    os.chmod(candidate_file, 0o600)
    root = tmp_path / "state"
    root.mkdir()
    state = {"archive_etag": '"same"', "archive_bytes": 456,
             "admitted": 500, "exact_deduped": 0,
             "next_shard": 2, "position": 9, "deferred": []}
    (root / "cursor.json").write_text(json.dumps(state))
    monkeypatch.setattr(backfill.sender, "new_session", lambda *a: object())

    class Expired(Exception):
        response = SimpleNamespace(status_code=403)

    def source(session, url, **kwargs):
        if url == old_url:
            raise Expired()
        return SimpleNamespace(etag='"same"', size=456)

    monkeypatch.setattr(backfill, "AuthenticatedRangeFile", source)
    result = backfill.run(url_file, maximum=100, state_root=root)
    assert result["state"] == "drive_lag_backpressure"
    assert url_file.read_text().strip() == new_url
    assert json.loads((root / "cursor.json").read_text())["position"] == 9
    assert not (root / "superseded-snapshots").exists()


def test_source_retry_after_skips_next_timer_network_read(monkeypatch, tmp_path):
    url_file = tmp_path / "privacy-export-url.txt"
    url_file.write_text("https://chatgpt.com/backend-api/estuary/content?source\n")
    os.chmod(url_file, 0o600)
    root = tmp_path / "state"
    monkeypatch.setattr(backfill.sender, "new_session", lambda *a: object())

    def limited(session, url, *, retry_hook):
        retry_hook(503, 600)
        raise backfill.ExportRetryLater(503, 600)

    monkeypatch.setattr(backfill, "AuthenticatedRangeFile", limited)
    try:
        backfill.run(url_file, maximum=1, state_root=root)
        assert False, "first provider read must defer"
    except backfill.ExportRetryLater:
        pass
    assert json.loads((root / "source-retry-after.json").read_text())["http_status"] == 503
    monkeypatch.setattr(backfill, "AuthenticatedRangeFile", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("provider read before Retry-After")))
    assert backfill.run(url_file, maximum=1, state_root=root)["state"] == "provider_retry_after"


def test_replacement_source_requires_same_authenticated_email(monkeypatch):
    import io
    import zipfile

    def archive(email):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as out:
            out.writestr("user.json", json.dumps({"email": email, "id": "distinct-user-id"}))
        buffer.seek(0)
        return buffer

    monkeypatch.setattr(backfill.sender, "identity", lambda session: {
        "user_email": "current@example.com", "account_id": "different-account-id"})
    backfill.verify_candidate_account(archive("CURRENT@example.com"), object())
    try:
        backfill.verify_candidate_account(archive("other@example.com"), object())
        assert False, "another account's export must not be promoted"
    except RuntimeError as exc:
        assert "different account" in str(exc)


def test_legacy_scope_repair_keeps_exact_events_and_drive_capture(monkeypatch):
    cid = "6ab2d995-14cc-83e9-b8e3-43d389f9a510"
    conversation = {"id": cid, "mapping": {
        "u": {"parent": None, "message": {"author": {"role": "user"},
            "content": {"parts": ["Ask"]}}},
        "a": {"parent": "u", "message": {"author": {"role": "assistant"},
            "content": {"parts": ["Answer"]}}},
        "t": {"parent": "a", "message": {"author": {"role": "assistant"},
            "content": {"content_type": "thoughts", "thoughts": [{"summary": "omitted"}]}}},
    }}
    _, summary = backfill.normalize(conversation)
    capture = {"source_conversation_sha256": backfill.sha_json(conversation),
               "export_etag": '"etag"', "source_complete": False,
               "branch_graph_sha256": summary["branch_graph_sha256"],
               "drive_verified": True, "drive_object_sha256": "a" * 64}
    calls = []
    monkeypatch.setattr(backfill, "prior_conversation", lambda _: {"capture": capture.copy()})
    monkeypatch.setattr(backfill, "exact_readback", lambda *args: True)
    def post(body):
        calls.append(body)
        capture.update(body["capture"])
        return {"stored": True}
    monkeypatch.setattr(backfill.sender, "operator_memory_post", post)
    info = SimpleNamespace(filename="conversations-150.json", CRC=0)
    source = SimpleNamespace(etag='"etag"', size=42)
    assert backfill.admit(conversation, info, source, 0) == "exact_dedupe"
    assert len(calls) == 1 and calls[0]["events"] == []
    assert capture["source_text_projection_scope"] == "standard_message_text_excluding_thoughts"
    assert capture["source_omitted_thought_count"] == 1
    assert capture["drive_verified"] is True and capture["drive_object_sha256"] == "a" * 64
    assert backfill.admit(conversation, info, source, 0) == "exact_dedupe"
    assert len(calls) == 1


def test_legacy_scope_receipt_cursor_replays_one_member_without_new_admission(monkeypatch, tmp_path):
    rows = [{"id": f"6ab2d995-14cc-83e9-b8e3-43d389f9a51{i}", "mapping": {}} for i in range(2)]
    archive_path = tmp_path / "source.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("conversations-150.json", json.dumps(rows))
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo("conversations-150.json")
    source = SimpleNamespace(etag='"etag"', size=42)
    receipts = [{"archive_etag": source.etag, "archive_bytes": source.size,
                 "member": info.filename, "member_crc32": f"{info.CRC:08x}",
                 "offset": i, "conversation_id": row["id"],
                 **({"stored_conversation_id": row["id"]} if i else {}),
                 "source_conversation_sha256": backfill.sha_json(row)}
                for i, row in enumerate(rows)]
    (tmp_path / "d1_exact_receipts.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in receipts))
    state = {}
    state_path = tmp_path / "cursor.json"
    calls = []
    monkeypatch.setattr(backfill, "source_identity", lambda row, *_: (row["id"], row["id"], {"capture": {}}))
    monkeypatch.setattr(backfill, "admit", lambda row, *_: calls.append(row["id"]) or "exact_dedupe")
    with zipfile.ZipFile(archive_path) as archive:
        assert backfill.repair_legacy_scope(archive, source, tmp_path, state_path, state, maximum=1) == 1
        assert backfill.repair_legacy_scope(archive, source, tmp_path, state_path, state, maximum=1) == 1
    assert calls == [row["id"] for row in rows]
    assert not backfill.legacy_scope_repair_pending(tmp_path, state)

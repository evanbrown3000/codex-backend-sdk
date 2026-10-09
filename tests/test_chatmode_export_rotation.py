from pathlib import Path
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from chatmode_export_rotation import finish_promotion, is_expired_source_error, promote


def private_text(path: Path, value: str):
    path.write_text(value)
    os.chmod(path, 0o600)


def test_expired_snapshot_promotion_keeps_old_gap_and_exact_receipts(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    old_url = tmp_path / "privacy-export-url.txt"
    next_url = tmp_path / "privacy-export-next-url.txt"
    private_text(old_url, "https://chatgpt.com/backend-api/estuary/content?old\n")
    private_text(next_url, "https://chatgpt.com/backend-api/estuary/content?new\n")
    state = {"archive_etag": '"old-etag"', "archive_bytes": 400,
             "next_shard": 9, "position": 17, "admitted": 120,
             "deferred": [{"shard": "conversations-009.json", "offset": 5}]}
    (root / "cursor.json").write_text(json.dumps(state))
    old_receipts = '{"archive_etag":"old-etag","conversation_id":"cid"}\n'
    (root / "d1_exact_receipts.jsonl").write_text(old_receipts)
    replacement = SimpleNamespace(etag='"new-etag"', size=500)
    archive = promote(root, old_url, next_url, state,
                      "https://chatgpt.com/backend-api/estuary/content?old",
                      "https://chatgpt.com/backend-api/estuary/content?new",
                      replacement, RuntimeError("HTTP 403"))
    gap = json.loads((archive / "gap-manifest.json").read_text())
    assert gap["source_fully_traversed"] is False
    assert gap["source_retirement_allowed"] is False
    assert gap["replacement_covers_old_gaps"] is None
    assert gap["position"] == 17 and gap["deferred"] == state["deferred"]
    assert (archive / "d1_exact_receipts.jsonl").read_text() == old_receipts
    assert old_url.read_text() == next_url.read_text()
    assert not (root / "cursor.json").exists()
    assert not (root / "d1_exact_receipts.jsonl").exists()
    assert not (root / "promotion.json").exists()
    assert (archive / "source-url.txt").stat().st_mode & 0o077 == 0


def test_interrupted_promotion_finishes_without_deleting_archived_custody(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    target = root / "superseded-snapshots" / "old"
    target.mkdir(parents=True)
    (target / "gap-manifest.json").write_text("{}")
    old_url = tmp_path / "privacy-export-url.txt"
    next_url = tmp_path / "privacy-export-next-url.txt"
    private_text(old_url, "old\n")
    private_text(next_url, "new\n")
    from hashlib import sha256
    (root / "cursor.json").write_text("old state")
    (root / "promotion.json").write_text(json.dumps({
        "old_snapshot": "old", "candidate_url_sha256": sha256(next_url.read_bytes()).hexdigest()}))
    finish_promotion(root, old_url, next_url)
    finish_promotion(root, old_url, next_url)
    assert old_url.read_text() == "new\n"
    assert not (root / "cursor.json").exists()
    assert (target / "gap-manifest.json").is_file()


def test_only_source_expiry_statuses_trigger_rotation():
    assert is_expired_source_error(SimpleNamespace(response=SimpleNamespace(status_code=403)))
    assert is_expired_source_error(SimpleNamespace(response=SimpleNamespace(status_code=404)))
    assert not is_expired_source_error(SimpleNamespace(response=SimpleNamespace(status_code=500)))
    assert not is_expired_source_error(TimeoutError())

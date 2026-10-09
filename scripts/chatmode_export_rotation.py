"""Crash-resumable custody when a privacy-export link expires mid-backfill.

The old source is never called complete by promotion.  Its cursor, exact D1
receipts, and unresolved offsets remain in a private snapshot directory.  A
new export begins at its own cursor and traverses all shards, so overlap is
deduplicated by the normal D1 admission path.  We do not infer that records
missing from the replacement were safely recovered.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

SOURCE_FILES = ("cursor.json", "d1_exact_receipts.jsonl",
                "nonconversation_source_items.jsonl", "skipped_source_conversations.jsonl",
                "retirement-checkpoint.json")


def _atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _atomic_json(path: Path, value: dict):
    _atomic_bytes(path, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())


def is_expired_source_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    return status in {400, 401, 403, 404, 410}


def _snapshot_name(state: dict) -> str:
    return sha256(f'{state["archive_etag"]}:{state["archive_bytes"]}'.encode()).hexdigest()[:24]


def _prepare_archive(root: Path, state: dict, old_url: str, new_source,
                     failure: Exception) -> Path:
    snapshots = root / "superseded-snapshots"
    snapshots.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = snapshots / _snapshot_name(state)
    if target.exists():
        existing = json.loads((target / "gap-manifest.json").read_text())
        if existing["archive_etag"] != state["archive_etag"]:
            raise RuntimeError("snapshot archive identity collision")
        return target
    tmp = snapshots / (target.name + f".{os.getpid()}.tmp")
    tmp.mkdir(mode=0o700)
    for name in SOURCE_FILES:
        source = root / name
        if source.exists():
            shutil.copyfile(source, tmp / name)
            os.chmod(tmp / name, 0o600)
            with (tmp / name).open("rb") as data:
                os.fsync(data.fileno())
    _atomic_bytes(tmp / "source-url.txt", (old_url + "\n").encode())
    _atomic_json(tmp / "gap-manifest.json", {
        "schema": "cognilode.chatmode.export_source_gap.v1",
        "archive_etag": state["archive_etag"], "archive_bytes": state["archive_bytes"],
        "last_completed_shard": state.get("last_completed_shard"),
        "next_shard": state.get("next_shard"), "position": state.get("position"),
        "deferred": state.get("deferred") or [],
        "d1_admitted": state.get("admitted", 0),
        "d1_exact_deduped": state.get("exact_deduped", 0),
        "source_fully_traversed": state.get("next_shard") == -1 and not state.get("deferred"),
        "drive_fully_verified": False,
        "source_retirement_allowed": False,
        "failure_type": type(failure).__name__,
        "replacement_archive_etag": new_source.etag,
        "replacement_archive_bytes": new_source.size,
        "replacement_covers_old_gaps": None,
    })
    fd = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    fd = os.open(snapshots, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


def finish_promotion(root: Path, url_file: Path, candidate_file: Path) -> None:
    """Idempotently finish a journaled promotion after any process crash."""
    journal_path = root / "promotion.json"
    if not journal_path.exists():
        return
    journal = json.loads(journal_path.read_text())
    target = root / "superseded-snapshots" / journal["old_snapshot"]
    if not (target / "gap-manifest.json").is_file():
        raise RuntimeError("promotion journal lacks archived source custody")
    frozen_candidate = root / "promotion-candidate-url.txt"
    candidate_source = frozen_candidate if frozen_candidate.is_file() else candidate_file
    if not candidate_source.is_file():
        raise RuntimeError("promotion candidate URL disappeared")
    candidate = candidate_source.read_bytes()
    if sha256(candidate).hexdigest() != journal["candidate_url_sha256"]:
        raise RuntimeError("promotion candidate URL changed")
    _atomic_bytes(url_file, candidate)
    for name in SOURCE_FILES:
        (root / name).unlink(missing_ok=True)
    journal_path.unlink()
    frozen_candidate.unlink(missing_ok=True)


def promote(root: Path, url_file: Path, candidate_file: Path, state: dict,
            old_url: str, candidate_url: str, new_source, failure: Exception) -> Path:
    """Called under the backfill's exclusive cursor lock."""
    archive = _prepare_archive(root, state, old_url, new_source, failure)
    candidate = (candidate_url + "\n").encode()
    frozen_candidate = root / "promotion-candidate-url.txt"
    _atomic_bytes(frozen_candidate, candidate)
    _atomic_json(root / "promotion.json", {
        "schema": "cognilode.chatmode.export_promotion.v1",
        "old_snapshot": archive.name,
        "candidate_url_sha256": sha256(candidate).hexdigest(),
        "new_archive_etag": new_source.etag,
        "new_archive_bytes": new_source.size,
    })
    finish_promotion(root, url_file, candidate_file)
    return archive

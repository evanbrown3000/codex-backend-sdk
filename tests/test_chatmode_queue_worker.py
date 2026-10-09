from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cognilode-chatmode-queue-worker"


def worker():
    loader = importlib.machinery.SourceFileLoader("chatmode_queue_worker_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_only_fenced_rhythm_claim_for_selected_device_is_sendable():
    module = worker()
    job = {"provider": "chatgpt.com", "rhythm_tape_sha256": "a" * 64,
           "rhythm_slot_index": 0, "claimed_by": "rhythm:evanpc:aaaa:0"}
    assert module.selected_for_device(job, "evanpc")
    assert not module.selected_for_device(job, "laptop")
    assert not module.selected_for_device({**job, "claimed_by": "ordinary-worker"}, "evanpc")
    assert not module.selected_for_device({**job, "rhythm_slot_index": None}, "evanpc")


def test_attachment_is_physically_present_and_hash_verified(tmp_path):
    module = worker()
    path = tmp_path / "research.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plan.plan", "[ ] step")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert module.attachment_paths({"attachment_refs": [{"ref": str(path), "sha256": digest}]}) == [path]
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        module.attachment_paths({"attachment_refs": [{"ref": str(path), "sha256": "0" * 64}]})


def test_completion_requires_terminal_central_readback_and_exact_zip(tmp_path):
    module = worker()
    path = tmp_path / "work.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("EXTERNAL_EFFECT_INSTRUCTIONS.md", "Deploy and verify")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    value = {"assistant_terminal": True, "conversation_id": "conv-1",
             "central_conversation_store": {"central_readback_verified": True, "conversation_id": "conv-1"},
             "downloaded_files": [{"path": str(path), "sha256": digest, "name": "work.zip"}]}
    assert module.validated_result(value)[0] == "conv-1"
    assert module.validated_result({**value, "assistant_terminal": False}) is None
    assert module.validated_result({**value, "central_conversation_store": {"central_readback_verified": False}}) is None
    assert module.validated_result({**value, "downloaded_files": [{"path": str(path), "sha256": "0" * 64, "name": "work.zip"}]}) is None
    missing = tmp_path / "missing-instructions.zip"
    with zipfile.ZipFile(missing, "w") as archive:
        archive.writestr("report.txt", "incomplete handoff")
    assert module.validated_result({**value, "downloaded_files": [{"path": str(missing),
        "sha256": hashlib.sha256(missing.read_bytes()).hexdigest(), "name": missing.name}]}) is None

import copy
import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-taskflow-amend-queued-attachment"
loader = importlib.machinery.SourceFileLoader("taskflow_attachment_amend_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = module
loader.exec_module(module)


class FakeD1:
    def __init__(self, job):
        self.job = job
        self.operations = []

    def operator_memory_post(self, body):
        self.operations.append(body["operation"])
        if body["operation"] == "get_job":
            return {"ok": True, "job": copy.deepcopy(self.job)}
        assert body["operation"] == "amend_queued_job_attachments"
        assert self.job["state"] == "queued"
        assert self.job["rhythm_slot_index"] is None
        assert body["expected_prompt_sha256"] == self.job["prompt_sha256"]
        assert body["expected_plan_revision"] == self.job["plan_revision"]
        assert body["expected_attachment_sha256"] == self.job["attachment_refs"][0]["sha256"]
        ref = body["attachment_ref"]
        present = any(row.get("sha256") == ref["sha256"] for row in self.job["attachment_refs"])
        if not present:
            self.job["attachment_refs"].append(ref)
        return {"ok": True, "idempotent": present, "job": copy.deepcopy(self.job)}


def fixture(tmp_path):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("RUN_ME.sh", "echo native sandbox\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    original = "a" * 64
    args = SimpleNamespace(job_id="tf-job-1", project="recursive-rpe-native-sandbox",
                           step="RPE-1", zip=source, zip_sha256=digest,
                           stage_path=tmp_path / "stable" / "RPE_NATIVE_SANDBOX_BOOTSTRAP_RUN_ME.zip",
                           bucket="example-bucket", name="Run RUN_ME.sh in native sandbox",
                           expected_prompt_sha256="b" * 64, expected_plan_sha256="c" * 64,
                           expected_original_sha256=original)
    job = {"id": args.job_id, "state": "queued", "rhythm_slot_index": None,
           "provider": "chatgpt.com", "prompt_author": "taskflow_plan",
           "project": args.project, "taskflow_step": args.step, "phase": "chatgpt_sandbox",
           "prompt": "exact plan prompt", "prompt_sha256": args.expected_prompt_sha256,
           "plan_revision": args.expected_plan_sha256,
           "due_at": "2026-10-09T05:51:38Z", "priority": 77,
           "assigned_employee": "Elliot Mercer", "reason": "decisionx_remote_daemon",
           "attachment_refs": [{"ref": "file:/research.zip", "sha256": original}]}
    controller = SimpleNamespace(portable_attachment_ref=lambda path, sha: {
        "ref": "file:" + str(path), "sha256": sha,
        "mirrors": [f"s3://{args.bucket}/taskflow-artifacts/sha256/{sha}.zip"]})
    return args, FakeD1(job), controller


def test_cas_amend_preserves_existing_job_and_is_idempotent(tmp_path):
    args, d1, controller = fixture(tmp_path)
    first = module.amend(args, sender=d1, controller=controller)
    assert first["attachment_count"] == 2
    assert first["idempotent"] is False
    assert first["attachment"]["sha256"] == args.zip_sha256
    assert hashlib.sha256(args.stage_path.read_bytes()).hexdigest() == args.zip_sha256
    assert d1.job["due_at"] == "2026-10-09T05:51:38Z"
    assert d1.operations == ["get_job", "amend_queued_job_attachments", "get_job"]
    second = module.amend(args, sender=d1, controller=controller)
    assert second["attachment_count"] == 2
    assert second["idempotent"] is True
    assert all(operation in {"get_job", "amend_queued_job_attachments"} for operation in d1.operations)


def test_bad_zip_and_claimed_job_cannot_amend(tmp_path):
    args, d1, controller = fixture(tmp_path)
    args.zip_sha256 = "d" * 64
    with pytest.raises(ValueError, match="ZIP path, size, or SHA-256"):
        module.amend(args, sender=d1, controller=controller)
    assert d1.operations == []
    args.zip_sha256 = hashlib.sha256(args.zip.read_bytes()).hexdigest()
    d1.job["state"] = "claimed"
    with pytest.raises(ValueError, match="preclaim fence"):
        module.amend(args, sender=d1, controller=controller)
    assert d1.operations == ["get_job"]
    assert not args.stage_path.exists()


def test_wrong_original_sha_or_rhythm_slot_cannot_amend(tmp_path):
    args, d1, controller = fixture(tmp_path)
    args.expected_original_sha256 = "e" * 64
    with pytest.raises(ValueError, match="original TaskFlow attachment"):
        module.amend(args, sender=d1, controller=controller)
    args.expected_original_sha256 = "a" * 64
    d1.job["rhythm_slot_index"] = 12
    with pytest.raises(ValueError, match="preclaim fence"):
        module.amend(args, sender=d1, controller=controller)
    assert not args.stage_path.exists()


def test_unsafe_zip_member_cannot_reach_d1(tmp_path):
    args, d1, controller = fixture(tmp_path)
    with zipfile.ZipFile(args.zip, "w") as archive:
        archive.writestr("../escape.txt", "outside")
    args.zip_sha256 = hashlib.sha256(args.zip.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="ZIP members"):
        module.amend(args, sender=d1, controller=controller)
    assert d1.operations == []

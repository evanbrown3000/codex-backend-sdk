from __future__ import annotations

import hashlib
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from test_taskflow_phase_controller import c


class Queue:
    def __init__(self, job):
        self.job = job

    def get(self, job_id):
        return self.job if job_id == self.job["id"] else None


def _fixture(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    originals = {
        "scripts/probe-recursive-rpe-native-effect": b"#!/usr/bin/python3\nprint('behavioral-probe')\n",
        "scripts/_rpe_native_behavior_fixture.py": b"def exercise():\n    assert 2 + 2 == 4\n",
    }
    for relative, body in originals.items():
        (repo / relative).write_bytes(body)
    source = tmp_path / "pre-native-source.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, body in originals.items():
            archive.writestr("research-plan-execute/" + relative, body)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    step = c.Step("RPE-1", "source-aware research", False, 0)
    plan = c.Plan("recursive-rpe-native-sandbox", "RPE", "a" * 64, "plan", (step,),
                  source_ref="file:" + str(repo / "plans" / "rpe.plan"))
    job = {
        "id": c.phase_job_id(plan, step.step_id, "chatgpt_sandbox"),
        "state": "queued", "conversation_id": None,
        "attachment_refs": [{"ref": "file:" + str(source), "sha256": digest}],
    }
    return repo, source, plan, step, Queue(job)


def test_rpe_probe_and_fixture_frozen_from_pre_native_zip_and_checked_after_claim(tmp_path: Path):
    repo, _, plan, step, queue = _fixture(tmp_path)
    with mock.patch.object(c, "execution_repo", return_value=repo):
        lock = c.freeze_rpe_probe_before_native(queue, plan, step, tmp_path / "state")
        assert lock["file_sha256s"]["scripts/_rpe_native_behavior_fixture.py"]
        queue.job["state"] = "complete"
        queue.job["conversation_id"] = "real-chat-conversation"
        assert c.verify_rpe_probe_custody(queue, plan, step, tmp_path / "state") == lock
        (repo / "scripts/_rpe_native_behavior_fixture.py").write_text(
            "def exercise():\n    pass\n")
        with pytest.raises(ValueError, match="fixture changed"):
            c.verify_rpe_probe_custody(queue, plan, step, tmp_path / "state")


def test_cannot_first_freeze_after_chatgpt_native_claim(tmp_path: Path):
    repo, _, plan, step, queue = _fixture(tmp_path)
    queue.job["state"] = "leased"
    queue.job["claimed_at"] = "2026-10-09T16:46:51Z"
    with mock.patch.object(c, "execution_repo", return_value=repo):
        with pytest.raises(ValueError, match="after native claim"):
            c.freeze_rpe_probe_before_native(queue, plan, step, tmp_path / "state")


def test_pre_native_zip_must_match_both_current_probe_and_fixture(tmp_path: Path):
    repo, source, plan, step, queue = _fixture(tmp_path)
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("research-plan-execute/scripts/probe-recursive-rpe-native-effect",
                         (repo / "scripts/probe-recursive-rpe-native-effect").read_bytes())
        archive.writestr("research-plan-execute/scripts/_rpe_native_behavior_fixture.py",
                         b"def exercise(): pass\n")
    queue.job["attachment_refs"][0]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    with mock.patch.object(c, "execution_repo", return_value=repo):
        with pytest.raises(ValueError, match="no source ZIP matching"):
            c.freeze_rpe_probe_before_native(queue, plan, step, tmp_path / "state")

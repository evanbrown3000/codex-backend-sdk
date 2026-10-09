"""An incompressible >20 MiB native work ZIP crosses the real result path.

The same ZIP is deliberately rejected as a prompt-upload attachment.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_native_result(path, plan_sha):
    binary = os.urandom(20 * 1024 * 1024 + 65536)
    files = {"EXTERNAL_EFFECT_INSTRUCTIONS.md": b"Install the verified work.",
             "codex": binary}
    manifest = {"plan_sha256": plan_sha,
                "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                          for name, content in files.items()]}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("MANIFEST.json", json.dumps(manifest))
    assert path.stat().st_size > 20 * 1024 * 1024


class MetadataResponse:
    status_code = 200
    headers = {}

    def json(self):
        return {"download_url": "https://download.invalid/native-work.zip"}


class StreamingResponse:
    status_code = 200

    def __init__(self, path):
        self.path = path
        self.headers = {"Content-Length": str(path.stat().st_size)}
        self.closed = False

    @property
    def content(self):
        raise AssertionError("returned ZIP must stream, not materialize response.content")

    def iter_content(self, chunk_size=1024 * 1024):
        with self.path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(chunk_size), b""):
                yield chunk

    def close(self):
        self.closed = True


class Session:
    def __init__(self, path):
        self.path = path
        self.download = StreamingResponse(path)

    def get(self, url, **kwargs):
        if "interpreter/download?" in url:
            return MetadataResponse()
        assert url == "https://download.invalid/native-work.zip"
        assert kwargs.get("stream") is True
        return self.download


def test_large_native_result_streams_mirrors_and_reaches_external_job(tmp_path):
    sender = load("large_result_sender", ROOT / "scripts/cognilode-b4pt0r-chatmode")
    worker = load("large_result_worker", ROOT / "scripts/cognilode-chatmode-queue-worker")
    controller = load("large_result_controller", ROOT / "scripts/cognilode-taskflow-phase-controller")
    plan_path = tmp_path / "native.plan"
    plan_path.write_text("project Native Port\nid native-port\n[ ] MC162-1 build and deploy\n")
    plan = controller.parse_plan(plan_path)
    source = tmp_path / "native-work.zip"
    make_native_result(source, plan.sha256)

    session = Session(source)
    downloaded = sender.download_interpreter_artifacts(
        session, {"access_token": "test", "account_id": "test"},
        conversation_id="conversation-1", assistant_message_id="assistant-1",
        assistant_text="sandbox:/mnt/data/native-work.zip",
        output_dir=tmp_path / "collected", device_id="device-1")
    assert session.download.closed
    assert len(downloaded) == 1
    collected = Path(downloaded[0]["path"])
    digest = downloaded[0]["sha256"]
    assert collected.stat().st_size > worker.MAX_ATTACHMENT_BYTES
    assert digest == sha_file(source) == sha_file(collected)
    with pytest.raises(ValueError, match="too large"):
        controller.safe_zip_members(collected)
    value = {"assistant_terminal": True, "conversation_id": "conversation-1",
             "central_conversation_store": {"central_readback_verified": True,
                                            "conversation_id": "conversation-1"},
             "downloaded_files": downloaded}
    assert worker.validated_result(value)[0] == "conversation-1"

    private = tmp_path / "private.zip"
    calls = []

    def aws_run(argv, **kwargs):
        action = argv[2]
        calls.append(action)
        if action == "head-object":
            if not private.exists():
                return subprocess.CompletedProcess(argv, 1, "", "missing")
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "ContentLength": private.stat().st_size,
                "Metadata": {"sha256": digest}}), "")
        if action == "put-object":
            shutil.copyfile(collected, private)
        if action == "get-object":
            shutil.copyfile(private, argv[-3])
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    with mock.patch.dict(os.environ, {"COGNILODE_TASKFLOW_ATTACHMENT_S3_BUCKET": "private-test-bucket",
                                  "COGNILODE_AWS_CLI": "aws"}), \
         mock.patch.object(worker.subprocess, "run", side_effect=aws_run), \
         mock.patch.object(worker, "ROOT", tmp_path / "worker"), \
         mock.patch.object(worker, "STAGE_ROOT", tmp_path / "other-device"):
        mirror = worker.publish_taskflow_work_zip(collected, digest)
        assert mirror == f"s3://private-test-bucket/taskflow-artifacts/sha256/{digest}.zip"
        staged = worker._stage_remote_attachment(mirror, digest,
                                                   max_bytes=controller.MAX_RETURNED_WORK_BYTES)
        assert sha_file(staged) == digest
        metadata = controller.verify_manifest_zip(staged, expected_plan_sha256=plan.sha256,
                                                  require_external_instructions=True,
                                                  returned_work=True)
        assert metadata["sha256"] == digest
        with pytest.raises(ValueError, match="content-addressed"):
            controller.portable_attachment_ref(staged, digest)
        ref = controller.portable_attachment_ref(staged, digest, returned_work=True)
        assert ref["sha256"] == digest and ref["mirrors"] == [mirror]

    assert calls.count("put-object") == 1
    assert calls.count("get-object") == 2


def test_interrupted_returned_download_keeps_no_truncated_artifact(tmp_path):
    sender = load("interrupted_result_sender", ROOT / "scripts/cognilode-b4pt0r-chatmode")

    class Interrupted(StreamingResponse):
        def iter_content(self, chunk_size=1024 * 1024):
            yield b"partial"
            raise OSError("connection reset during linked-file download")

    class InterruptedSession(Session):
        def __init__(self, path):
            super().__init__(path)
            self.download = Interrupted(path)

    path = tmp_path / "source.zip"
    path.write_bytes(b"test")
    session = InterruptedSession(path)
    with pytest.raises(OSError, match="connection reset"):
        sender.download_interpreter_artifacts(
            session, {"access_token": "test", "account_id": "test"},
            conversation_id="conversation-1", assistant_message_id="assistant-1",
            assistant_text="sandbox:/mnt/data/work.zip",
            output_dir=tmp_path / "collected", device_id="device-1")
    assert session.download.closed
    assert not list((tmp_path / "collected").glob("*.part"))
    assert not list((tmp_path / "collected").glob("*.zip"))

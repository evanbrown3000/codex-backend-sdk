"""Provider attachment custody through the Universe Storage network API."""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
from urllib.parse import quote


def _password() -> str:
    value = os.environ.get("UNIVERSE_STORAGE_OPERATOR_PASSWORD", "").strip()
    path = os.environ.get("UNIVERSE_STORAGE_OPERATOR_PASSWORD_FILE", "").strip()
    if not value and path:
        value = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("unified storage operator custody is not configured")
    return value


def commit_bytes(
    name: str,
    data: bytes,
    *,
    root_env: str = "COGNILODE_PROMPT_ATTACHMENT_ROOT",
    purpose: str = "provider-prompt-attachment",
) -> dict[str, Any]:
    endpoint = os.environ.get("UNIVERSE_STORAGE_ENDPOINT", "").strip()
    root = os.environ.get(root_env, "").rstrip("/")
    if not root and root_env == "COGNILODE_RETURNED_ARTIFACT_ROOT":
        prompt_root = os.environ.get("COGNILODE_PROMPT_ATTACHMENT_ROOT", "").rstrip("/")
        root = prompt_root + "/returned" if prompt_root else ""
    digest = hashlib.sha256(data).hexdigest()
    if not endpoint or not root:
        # The EvanPC Electron app and company containers address the same
        # durable queue-input mount through different filesystem paths.  Store
        # once beneath that shared mount and publish the stable container
        # locator.  Configured Universe Storage remains the first choice.
        physical_roots = [
            Path(os.environ.get("COGNILODE_QUEUE_INPUT_SHARED_ROOT", "/runtime/queue-inputs")),
            Path.home() / ".local/share/cognilode/company-runtime/shared/queue-inputs",
        ]
        shared = next((candidate for candidate in physical_roots
                       if candidate.parent.is_dir() and os.access(candidate.parent, os.W_OK)), None)
        if shared is not None:
            safe_name = Path(name).name or "attachment"
            destination = shared / digest / safe_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    staged = Path(temporary.name)
                try:
                    os.replace(staged, destination)
                finally:
                    staged.unlink(missing_ok=True)
            return {
                "ref": f"file:/runtime/queue-inputs/{digest}/{safe_name}",
                "sha256": digest,
                "name": safe_name,
                "size": len(data),
            }
        bucket = os.environ.get(
            "COGNILODE_TASKFLOW_ATTACHMENT_S3_BUCKET",
            "cognilode-ephemeral-artifacts-362928919715-us-west-2",
        ).strip()
        if not bucket:
            raise RuntimeError("unified prompt attachment storage is not configured")
        key = f"taskflow-artifacts/sha256/{digest}.zip"
        with tempfile.NamedTemporaryFile() as source:
            source.write(data)
            source.flush()
            completed = subprocess.run(
                [os.environ.get("COGNILODE_AWS_CLI", "aws"), "s3api", "put-object",
                 "--bucket", bucket, "--key", key, "--body", source.name,
                 "--metadata", f"sha256={digest}", "--content-type",
                 mimetypes.guess_type(name)[0] or "application/octet-stream"],
                capture_output=True, text=True, timeout=180, check=False,
            )
        if completed.returncode:
            raise RuntimeError("unified attachment launchpad write failed: " + completed.stderr[-500:])
        return {"ref": f"s3://{bucket}/{key}", "sha256": digest,
                "name": name, "size": len(data)}
    try:
        from universe_storage.client import UniverseStorageClient
    except ImportError as exc:
        raise RuntimeError("universe-storage client is required for attachment custody") from exc
    locator = f"{root}/sha256/{digest}/{quote(name, safe='._-')}"
    stored = UniverseStorageClient(
        endpoint, operator_password=_password(), timeout=180
    ).commit(
        locator,
        data,
        media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
        metadata={"sha256": digest, "name": name, "purpose": purpose},
    )
    return {
        "ref": stored.locator.to_uri(),
        "sha256": digest,
        "name": name,
        "size": len(data),
    }


def commit_path(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"attachment is not a regular file: {source}")
    return commit_bytes(source.name, source.read_bytes())


def commit_returned_artifact(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"artifact is not a regular file: {source}")
    return commit_bytes(
        source.name,
        source.read_bytes(),
        root_env="COGNILODE_RETURNED_ARTIFACT_ROOT",
        purpose="provider-returned-artifact",
    )

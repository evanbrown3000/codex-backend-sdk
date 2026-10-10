"""Provider attachment custody through the Universe Storage network API."""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
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


def commit_bytes(name: str, data: bytes) -> dict[str, Any]:
    try:
        from universe_storage.client import UniverseStorageClient
    except ImportError as exc:
        raise RuntimeError("universe-storage client is required for attachment custody") from exc
    endpoint = os.environ.get("UNIVERSE_STORAGE_ENDPOINT", "").strip()
    root = os.environ.get("COGNILODE_PROMPT_ATTACHMENT_ROOT", "").rstrip("/")
    if not endpoint or not root:
        raise RuntimeError("unified prompt attachment storage is not configured")
    digest = hashlib.sha256(data).hexdigest()
    locator = f"{root}/sha256/{digest}/{quote(name, safe='._-')}"
    stored = UniverseStorageClient(
        endpoint, operator_password=_password(), timeout=180
    ).commit(
        locator,
        data,
        media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
        metadata={"sha256": digest, "name": name, "purpose": "provider-prompt-attachment"},
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

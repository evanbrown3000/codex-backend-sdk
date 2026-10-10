"""Immutable source/runtime boundary for provider mutations."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Mapping


class GenerationMismatch(RuntimeError):
    """The executing provider process is not the declared generation."""


@dataclass(frozen=True)
class GenerationManifest:
    generation_id: str
    source_revision: str
    runtime_fingerprint: str
    provider_transport_version: str
    fence_identity: str
    runtime_python: str
    executable_root: str

    @classmethod
    def load(cls, path: str | None = None) -> "GenerationManifest":
        manifest_path = Path(path or os.environ.get("COGNILODE_GENERATION_MANIFEST", ""))
        if not manifest_path.is_file():
            raise GenerationMismatch("immutable_generation_manifest_missing")
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GenerationMismatch("immutable_generation_manifest_invalid") from exc
        if not isinstance(value, dict):
            raise GenerationMismatch("immutable_generation_manifest_invalid")
        return cls(**{name: str(value.get(name, "")) for name in cls.__dataclass_fields__})

    def validate(self) -> None:
        if not all(getattr(self, name) for name in self.__dataclass_fields__):
            raise GenerationMismatch("immutable_generation_manifest_incomplete")
        root = Path(self.executable_root).resolve()
        if Path(sys.executable).resolve() != Path(self.runtime_python).resolve():
            raise GenerationMismatch("python_interpreter_generation_mismatch")
        runtime_fingerprint = hashlib.sha256(
            f"{self.source_revision}|{self.runtime_python}".encode()
        ).hexdigest()
        if self.runtime_fingerprint != runtime_fingerprint:
            raise GenerationMismatch("runtime_fingerprint_manifest_mismatch")
        expected = os.environ.get("COGNILODE_RUNTIME_GENERATION_FINGERPRINT", "")
        if expected and expected != self.runtime_fingerprint:
            raise GenerationMismatch("source_runtime_generation_mismatch")
        expected = os.environ.get("COGNILODE_PROVIDER_TRANSPORT_VERSION", "")
        if expected and expected != self.provider_transport_version:
            raise GenerationMismatch("provider_transport_generation_mismatch")
        expected = os.environ.get("COGNILODE_GENERATION_FENCE_IDENTITY", "")
        if expected and expected != self.fence_identity:
            raise GenerationMismatch("generation_fence_identity_mismatch")
        transport = root / "scripts/cognilode-provider-transport"
        try:
            version = hashlib.sha256(transport.read_bytes()).hexdigest()
        except OSError as exc:
            raise GenerationMismatch("provider_transport_generation_unreadable") from exc
        if version != self.provider_transport_version:
            raise GenerationMismatch("provider_transport_source_mismatch")


def require_generation(fence: Mapping[str, object] | None = None) -> GenerationManifest:
    """Validate the immutable generation and an effect/capability fence."""
    manifest = GenerationManifest.load()
    manifest.validate()
    if not isinstance(fence, Mapping) or not (
        fence.get("lease_token") or fence.get("capability")
    ):
        raise GenerationMismatch("d1_fence_missing_at_execution_boundary")
    return manifest

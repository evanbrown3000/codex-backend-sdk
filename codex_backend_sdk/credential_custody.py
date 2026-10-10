"""Encrypted, single-writer custody for ChatGPT company credentials.

The provider SDK already accepts an in-memory ``TokenStore``.  This module
therefore owns storage and process custody only; it never implements provider
HTTP calls.  Durable objects live behind the Universe storage adapter and the
one active broker materializes them into its private runtime filesystem.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Any, Iterable, Protocol, Sequence

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


ENVELOPE_MAGIC = b"COGNILODE-CREDENTIAL-CUSTODY-V1\n"
LEASE_SCHEMA = "cognilode.credential_custody.lease.v1"
MANIFEST_SCHEMA = "cognilode.credential_custody.bundle.v1"


class CustodyError(RuntimeError):
    """Custody operation could not preserve its invariants."""


class RevisionConflict(CustodyError):
    """The durable object changed after it was read."""


@dataclass(frozen=True)
class StoredObject:
    revision: str
    size: int


class CustodyStorage(Protocol):
    """Narrow join to the Universe storage adapter."""

    def stat(self, locator: str) -> StoredObject | None: ...
    def read(self, locator: str, destination: Path) -> StoredObject: ...
    def write(
        self, locator: str, source: Path, *, expected_revision: str | None
    ) -> StoredObject: ...
    def delete(self, locator: str, *, expected_revision: str | None) -> None: ...


class CommandStorage:
    """Use the unified storage CLI without adding a provider-specific adapter.

    The command receives one JSON request on stdin and returns one JSON object
    on stdout.  File bytes travel through paths, avoiding base64 expansion for
    large browser profiles.  Conditional writes are mandatory.
    """

    def __init__(self, command: Sequence[str], *, timeout: int = 900) -> None:
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("storage command must be a non-empty argv sequence")
        self.command = tuple(command)
        self.timeout = timeout

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            self.command,
            input=json.dumps(request, separators=(",", ":")) + "\n",
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        try:
            result = json.loads(completed.stdout)
        except (ValueError, TypeError) as exc:
            raise CustodyError("storage adapter returned no JSON result") from exc
        if completed.returncode != 0 or result.get("ok") is not True:
            if result.get("error") == "revision_conflict":
                raise RevisionConflict("storage object revision changed")
            raise CustodyError(str(result.get("error") or "storage adapter operation failed"))
        return result

    @staticmethod
    def _stored(result: dict[str, Any]) -> StoredObject:
        revision = str(result.get("revision") or "")
        if not revision:
            raise CustodyError("storage adapter omitted object revision")
        return StoredObject(revision=revision, size=int(result.get("size") or 0))

    def stat(self, locator: str) -> StoredObject | None:
        result = self._call({"operation": "stat", "locator": locator})
        if result.get("exists") is False:
            return None
        return self._stored(result)

    def read(self, locator: str, destination: Path) -> StoredObject:
        result = self._call(
            {"operation": "read", "locator": locator, "destination": str(destination)}
        )
        return self._stored(result)

    def write(
        self, locator: str, source: Path, *, expected_revision: str | None
    ) -> StoredObject:
        result = self._call(
            {
                "operation": "write",
                "locator": locator,
                "source": str(source),
                "expected_revision": expected_revision,
            }
        )
        return self._stored(result)

    def delete(self, locator: str, *, expected_revision: str | None) -> None:
        self._call(
            {
                "operation": "delete",
                "locator": locator,
                "expected_revision": expected_revision,
            }
        )


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_encryption_key(path: Path) -> bytes:
    """Load a generated 256-bit key; passwords are intentionally unsupported."""

    raw = path.read_bytes().strip()
    candidates = [raw]
    try:
        candidates.append(bytes.fromhex(raw.decode("ascii")))
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        candidates.append(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        pass
    for candidate in candidates:
        if len(candidate) == 32:
            return candidate
    raise CustodyError("credential custody key must contain exactly 256 generated bits")


def encrypt_file(source: Path, destination: Path, key: bytes) -> None:
    nonce = secrets.token_bytes(12)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(ENVELOPE_MAGIC)
    with source.open("rb") as reader, temporary.open("wb") as writer:
        os.chmod(temporary, 0o600)
        writer.write(ENVELOPE_MAGIC)
        writer.write(nonce)
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(encryptor.update(chunk))
        writer.write(encryptor.finalize())
        writer.write(encryptor.tag)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(temporary, destination)


def decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    size = source.stat().st_size
    minimum = len(ENVELOPE_MAGIC) + 12 + 16
    if size < minimum:
        raise CustodyError("credential custody envelope is truncated")
    with source.open("rb") as reader:
        if reader.read(len(ENVELOPE_MAGIC)) != ENVELOPE_MAGIC:
            raise CustodyError("credential custody envelope has the wrong schema")
        nonce = reader.read(12)
        reader.seek(-16, os.SEEK_END)
        tag = reader.read(16)
        remaining = size - minimum
        reader.seek(len(ENVELOPE_MAGIC) + 12)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(ENVELOPE_MAGIC)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".partial")
        with temporary.open("wb") as writer:
            os.chmod(temporary, 0o600)
            while remaining:
                chunk = reader.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise CustodyError("credential custody envelope ended early")
                remaining -= len(chunk)
                writer.write(decryptor.update(chunk))
            writer.write(decryptor.finalize())
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)


@dataclass(frozen=True)
class BundleEntry:
    logical_name: str
    kind: str
    archive_path: str
    source_fingerprint: str


@dataclass(frozen=True)
class BundleManifest:
    schema: str
    created_at: str
    source_environment: str
    entries: tuple[BundleEntry, ...]


def _archive_name(logical_name: str, source: Path) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in logical_name)
    if not safe or safe in {".", ".."}:
        raise CustodyError("credential logical name is invalid")
    suffix = ".directory" if source.is_dir() else ".file"
    return f"entries/{safe}{suffix}"


def _path_fingerprint(path: Path) -> str:
    if path.is_file():
        return _sha256(path)
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        relative = item.relative_to(path).as_posix().encode()
        stat = item.stat()
        digest.update(relative + b"\0" + str(stat.st_size).encode() + b"\0")
        # Credential JSON and browser identity databases must participate in
        # duplicate detection; large caches are identified by size and path.
        if stat.st_size <= 16 * 1024 * 1024:
            digest.update(bytes.fromhex(_sha256(item)))
    return digest.hexdigest()


def create_bundle(
    destination: Path,
    entries: Iterable[tuple[str, str, Path]],
    *,
    source_environment: str,
) -> BundleManifest:
    normalized: list[tuple[BundleEntry, Path]] = []
    for logical_name, kind, source in entries:
        source = source.expanduser().resolve(strict=True)
        archive_path = _archive_name(logical_name, source)
        normalized.append(
            (
                BundleEntry(
                    logical_name=logical_name,
                    kind=kind,
                    archive_path=archive_path,
                    source_fingerprint=_path_fingerprint(source),
                ),
                source,
            )
        )
    if not normalized:
        raise CustodyError("credential bundle cannot be empty")
    manifest = BundleManifest(
        schema=MANIFEST_SCHEMA,
        created_at=_utc(),
        source_environment=source_environment,
        entries=tuple(entry for entry, _ in normalized),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        payload = json.dumps(
            {**asdict(manifest), "entries": [asdict(e) for e in manifest.entries]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        info.mode = 0o600
        info.mtime = 0
        import io

        archive.addfile(info, io.BytesIO(payload))
        for entry, source in normalized:
            archive.add(source, arcname=entry.archive_path, recursive=True)
    return manifest


def _safe_extract(archive_path: Path, destination: Path) -> BundleManifest:
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
        if "manifest.json" not in names:
            raise CustodyError("credential bundle has no manifest")
        for member in members:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk():
                raise CustodyError("credential bundle contains an unsafe path")
        manifest_value = json.loads(archive.extractfile("manifest.json").read())
        if manifest_value.get("schema") != MANIFEST_SCHEMA:
            raise CustodyError("credential bundle manifest schema is unsupported")
        # Every member has already been constrained to a relative, non-link
        # path, so extraction is safe on every supported Python version.
        archive.extractall(destination)
    entries = tuple(BundleEntry(**value) for value in manifest_value.get("entries", ()))
    for entry in entries:
        materialized = destination / entry.archive_path
        if not materialized.exists() or _path_fingerprint(materialized) != entry.source_fingerprint:
            raise CustodyError("credential bundle readback does not match its manifest")
    return BundleManifest(
        schema=manifest_value["schema"],
        created_at=manifest_value["created_at"],
        source_environment=manifest_value["source_environment"],
        entries=entries,
    )


@dataclass(frozen=True)
class BrokerLease:
    schema: str
    broker_id: str
    environment_id: str
    generation: int
    expires_at: float


class CredentialCustody:
    def __init__(
        self,
        storage: CustodyStorage,
        *,
        bundle_locator: str,
        lease_locator: str,
        key: bytes,
    ) -> None:
        self.storage = storage
        self.bundle_locator = bundle_locator
        self.lease_locator = lease_locator
        self.key = key

    def move_into_custody(
        self,
        entries: Iterable[tuple[str, str, Path]],
        *,
        source_environment: str,
        expected_revision: str | None,
        remove_sources: bool = True,
    ) -> StoredObject:
        entries = tuple((name, kind, path.expanduser()) for name, kind, path in entries)
        with tempfile.TemporaryDirectory(prefix="credential-custody-admit-") as directory:
            root = Path(directory)
            archive = root / "bundle.tar.gz"
            encrypted = root / "bundle.enc"
            manifest = create_bundle(
                archive, entries, source_environment=source_environment
            )
            encrypt_file(archive, encrypted, self.key)
            stored = self.storage.write(
                self.bundle_locator, encrypted, expected_revision=expected_revision
            )
            readback = root / "readback.enc"
            observed = self.storage.read(self.bundle_locator, readback)
            if observed.revision != stored.revision or _sha256(readback) != _sha256(encrypted):
                raise CustodyError("central credential readback differs from committed object")
            verified_archive = root / "verified.tar.gz"
            decrypt_file(readback, verified_archive, self.key)
            verified_root = root / "verified"
            verified = _safe_extract(verified_archive, verified_root)
            if verified != manifest:
                raise CustodyError("central credential manifest differs after readback")
        if remove_sources:
            for _, _, source in entries:
                source = source.resolve(strict=True)
                if source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()
        return stored

    def materialize(self, destination: Path) -> tuple[BundleManifest, StoredObject]:
        if destination.exists() and any(destination.iterdir()):
            raise CustodyError("broker runtime destination must be empty")
        destination.mkdir(parents=True, exist_ok=True)
        os.chmod(destination, 0o700)
        with tempfile.TemporaryDirectory(prefix="credential-custody-read-") as directory:
            root = Path(directory)
            encrypted = root / "bundle.enc"
            stored = self.storage.read(self.bundle_locator, encrypted)
            archive = root / "bundle.tar.gz"
            decrypt_file(encrypted, archive, self.key)
            manifest = _safe_extract(archive, destination)
        return manifest, stored

    def acquire_lease(
        self,
        *,
        broker_id: str,
        environment_id: str,
        ttl_seconds: int = 90,
    ) -> BrokerLease:
        if ttl_seconds < 30:
            raise ValueError("broker lease must allow for storage latency")
        prior_revision: str | None = None
        prior: BrokerLease | None = None
        with tempfile.TemporaryDirectory(prefix="credential-custody-lease-") as directory:
            path = Path(directory) / "lease.json"
            current = self.storage.stat(self.lease_locator)
            if current is not None:
                self.storage.read(self.lease_locator, path)
                value = json.loads(path.read_text(encoding="utf-8"))
                prior = BrokerLease(**value)
                prior_revision = current.revision
            now = time.time()
            if prior and prior.expires_at > now and prior.broker_id != broker_id:
                raise CustodyError(
                    f"credential custody is held by broker {prior.broker_id}"
                )
            lease = BrokerLease(
                schema=LEASE_SCHEMA,
                broker_id=broker_id,
                environment_id=environment_id,
                generation=(prior.generation + 1 if prior else 1),
                expires_at=now + ttl_seconds,
            )
            path.write_text(json.dumps(asdict(lease), sort_keys=True), encoding="utf-8")
            os.chmod(path, 0o600)
            self.storage.write(
                self.lease_locator, path, expected_revision=prior_revision
            )
            return lease

    def renew_lease(self, lease: BrokerLease, *, ttl_seconds: int = 90) -> BrokerLease:
        current = self.storage.stat(self.lease_locator)
        if current is None:
            raise CustodyError("credential custody lease disappeared")
        with tempfile.TemporaryDirectory(prefix="credential-custody-renew-") as directory:
            path = Path(directory) / "lease.json"
            self.storage.read(self.lease_locator, path)
            observed = BrokerLease(**json.loads(path.read_text(encoding="utf-8")))
            if observed.broker_id != lease.broker_id or observed.generation != lease.generation:
                raise CustodyError("credential custody lease was fenced by another broker")
            renewed = BrokerLease(
                schema=LEASE_SCHEMA,
                broker_id=lease.broker_id,
                environment_id=lease.environment_id,
                generation=lease.generation,
                expires_at=time.time() + ttl_seconds,
            )
            path.write_text(json.dumps(asdict(renewed), sort_keys=True), encoding="utf-8")
            self.storage.write(
                self.lease_locator, path, expected_revision=current.revision
            )
            return renewed

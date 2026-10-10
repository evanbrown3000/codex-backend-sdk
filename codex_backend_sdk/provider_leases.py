"""Opaque, short-lived capabilities for credential-bearing provider work.

Provider credentials never cross this boundary.  A scheduler asks the custody
process for a capability scoped to one immutable operation.  Only the custody
process can redeem the capability, and redemption returns authorization to use
an account already materialized inside custody rather than the account secret.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any, Mapping


class LeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderCapabilityLease:
    lease_id: str
    token: str
    provider: str
    account_id: str
    operation_id: str
    operation: str
    issued_at: float
    expires_at: float
    generation: int

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("token", None)
        return value


class ProviderLeaseAuthority:
    """Single-process lease authority backed by a private SQLite ledger."""

    def __init__(self, path: str | Path, *, authority_secret: bytes | None = None) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self.secret = authority_secret or secrets.token_bytes(32)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS provider_accounts (
              provider TEXT NOT NULL,
              account_id TEXT NOT NULL,
              custody_ref TEXT NOT NULL,
              capabilities_json TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              generation INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY(provider, account_id)
            );
            CREATE TABLE IF NOT EXISTS capability_leases (
              lease_id TEXT PRIMARY KEY,
              token_sha256 TEXT NOT NULL,
              provider TEXT NOT NULL,
              account_id TEXT NOT NULL,
              operation_id TEXT NOT NULL UNIQUE,
              operation TEXT NOT NULL,
              issued_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              generation INTEGER NOT NULL,
              redeemed_at REAL,
              revoked_at REAL
            );
            CREATE TABLE IF NOT EXISTS lease_events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              lease_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              observed_at REAL NOT NULL,
              payload_json TEXT NOT NULL
            );
            """
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _provider(value: str) -> str:
        aliases = {
            "chatgpt": "chatgpt.com", "chatgpt.com": "chatgpt.com",
            "gemini": "gemini.com", "gemini.com": "gemini.com",
            "claude": "claude.com", "claude.com": "claude.com",
            "anthropic": "anthropic.com", "anthropic.com": "anthropic.com",
        }
        try:
            return aliases[value.strip().casefold()]
        except KeyError as exc:
            raise LeaseError("unsupported provider") from exc

    def register_account(
        self,
        *,
        provider: str,
        account_id: str,
        custody_ref: str,
        capabilities: Mapping[str, Any],
    ) -> None:
        """Register an opaque custody reference, never credential material."""
        selected = self._provider(provider)
        if not account_id or not custody_ref:
            raise LeaseError("account_id and custody_ref are required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.db.execute(
                "SELECT * FROM provider_accounts WHERE provider=? AND account_id=?",
                (selected, account_id),
            ).fetchone()
            encoded = json.dumps(dict(capabilities), sort_keys=True)
            unchanged = bool(
                current
                and current["custody_ref"] == custody_ref
                and current["capabilities_json"] == encoded
                and int(current["active"]) == 1
            )
            generation = int(current["generation"]) if unchanged else (
                int(current["generation"]) + 1 if current else 1
            )
            self.db.execute(
                """INSERT INTO provider_accounts
                   (provider,account_id,custody_ref,capabilities_json,active,generation)
                   VALUES(?,?,?,?,1,?)
                   ON CONFLICT(provider,account_id) DO UPDATE SET
                     custody_ref=excluded.custody_ref,
                     capabilities_json=excluded.capabilities_json,
                     active=1,generation=excluded.generation""",
                (selected, account_id, custody_ref, encoded, generation),
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def accounts(self, provider: str) -> list[dict[str, Any]]:
        selected = self._provider(provider)
        rows = self.db.execute(
            "SELECT * FROM provider_accounts WHERE provider=? AND active=1 ORDER BY account_id",
            (selected,),
        ).fetchall()
        return [
            {
                "provider": row["provider"],
                "account_id": row["account_id"],
                "capabilities": json.loads(row["capabilities_json"]),
                "generation": row["generation"],
            }
            for row in rows
        ]

    def issue(
        self,
        *,
        provider: str,
        account_id: str,
        operation_id: str,
        operation: str = "prompt",
        ttl_seconds: int = 120,
    ) -> ProviderCapabilityLease:
        if ttl_seconds < 10 or ttl_seconds > 900:
            raise LeaseError("lease lifetime must be between 10 and 900 seconds")
        selected = self._provider(provider)
        account = self.db.execute(
            "SELECT * FROM provider_accounts WHERE provider=? AND account_id=? AND active=1",
            (selected, account_id),
        ).fetchone()
        if account is None:
            raise LeaseError("provider account is not active in custody")
        raw = secrets.token_urlsafe(32)
        lease_id = secrets.token_urlsafe(18)
        now = time.time()
        generation = int(account["generation"])
        token = lease_id + "." + raw
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute(
                "SELECT * FROM capability_leases WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if prior:
                raise LeaseError("operation already has a capability lease")
            self.db.execute(
                """INSERT INTO capability_leases
                   (lease_id,token_sha256,provider,account_id,operation_id,operation,
                    issued_at,expires_at,generation)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (lease_id, token_hash, selected, account_id, operation_id, operation,
                 now, now + ttl_seconds, generation),
            )
            self._event(lease_id, "issued", {"operation_id": operation_id, "provider": selected})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return ProviderCapabilityLease(
            lease_id=lease_id, token=token, provider=selected, account_id=account_id,
            operation_id=operation_id, operation=operation, issued_at=now,
            expires_at=now + ttl_seconds, generation=generation,
        )

    def redeem(self, token: str, *, operation_id: str, operation: str = "prompt") -> dict[str, Any]:
        """Redeem once inside custody; returns an opaque local custody reference."""
        lease_id = token.partition(".")[0]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM capability_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if row is None or not hmac.compare_digest(row["token_sha256"], token_hash):
                raise LeaseError("invalid capability lease")
            if row["operation_id"] != operation_id or row["operation"] != operation:
                raise LeaseError("capability scope mismatch")
            if row["revoked_at"] is not None or row["redeemed_at"] is not None:
                raise LeaseError("capability lease is no longer usable")
            if float(row["expires_at"]) <= time.time():
                raise LeaseError("capability lease expired")
            account = self.db.execute(
                "SELECT * FROM provider_accounts WHERE provider=? AND account_id=? AND active=1",
                (row["provider"], row["account_id"]),
            ).fetchone()
            if account is None or int(account["generation"]) != int(row["generation"]):
                raise LeaseError("credential generation changed")
            self.db.execute(
                "UPDATE capability_leases SET redeemed_at=? WHERE lease_id=?",
                (time.time(), lease_id),
            )
            self._event(lease_id, "redeemed", {"operation_id": operation_id})
            self.db.execute("COMMIT")
            return {
                "provider": row["provider"],
                "account_id": row["account_id"],
                "custody_ref": account["custody_ref"],
                "capabilities": json.loads(account["capabilities_json"]),
                "generation": row["generation"],
                "lease_id": lease_id,
            }
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def revoke(self, lease_id: str, reason: str = "revoked") -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "UPDATE capability_leases SET revoked_at=? WHERE lease_id=? AND revoked_at IS NULL",
                (time.time(), lease_id),
            )
            self._event(lease_id, "revoked", {"reason": reason})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _event(self, lease_id: str, kind: str, payload: Mapping[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO lease_events(lease_id,kind,observed_at,payload_json) VALUES(?,?,?,?)",
            (lease_id, kind, time.time(), json.dumps(dict(payload), sort_keys=True)),
        )


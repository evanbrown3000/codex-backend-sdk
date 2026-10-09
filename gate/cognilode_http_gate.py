"""Process-side request admission through the container-hosted HTTP gate."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
from urllib.parse import urlsplit
import uuid


def _source() -> str:
    return os.environ.get("COGNILODE_HTTP_SOURCE") or Path(sys.argv[0] or "python").name


def _purpose(method: str, url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path[:512]
    if host == "chatgpt.com" or host.endswith(".chatgpt.com"):
        if path.startswith("/api/auth/"):
            return "credential_refresh", host, path
        if "/interpreter/download" in path or "/files/" in path:
            return "artifact_download", host, path
        if method in {"POST", "PUT", "PATCH"} and ("/conversation" in path or "/uploads" in path):
            return "chatmode_prompt_or_upload", host, path
        return "observability", host, path
    if host == "cognilode.com" and path.startswith("/api/operator/http-gate"):
        return "central_gate_control", host, path
    return "external_http", host, path


def _fallback(method: str, url: str, error: str) -> dict:
    request_purpose, host, path = _purpose(method, url)
    # A missing gate cannot authorize a provider request. The durable row lets
    # the central gate account for the refusal once its container restarts.
    allowed = False
    row = {"event_id": str(uuid.uuid4()), "at": datetime.now(timezone.utc).isoformat(),
           "environment": os.environ.get("COGNILODE_ENVIRONMENT_ID", "unknown"),
           "source": _source(), "purpose": request_purpose, "method": method,
           "host": host, "path": path, "allowed": allowed,
           "outcome": "durable_fallback:" + error[:80]}
    log = os.environ.get("COGNILODE_HTTP_GATE_FALLBACK_LOG", "")
    if log:
        file = Path(log)
        file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
    return {"ok": True, "allowed": allowed, "purpose": request_purpose,
            "event_id": row["event_id"], "fallback": True}


def decide(method: str, url: str) -> dict:
    method = method.upper()
    path = os.environ.get("COGNILODE_HTTP_GATE_SOCKET", "")
    if not path:
        return _fallback(method, url, "socket_unconfigured")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(path)
            body = {"method": method, "url": url, "source": _source()}
            sock.sendall((json.dumps(body, separators=(",", ":")) + "\n").encode())
            data = bytearray()
            while b"\n" not in data and len(data) < 65536:
                part = sock.recv(4096)
                if not part:
                    break
                data.extend(part)
        result = json.loads(bytes(data).split(b"\n", 1)[0])
        if result.get("ok") is not True:
            raise RuntimeError("gate rejected decision request")
        return result
    except Exception as exc:
        return _fallback(method, url, type(exc).__name__)


def require(method: str, url: str) -> None:
    decision = decide(method, url)
    if not decision.get("allowed"):
        raise PermissionError("central HTTP gate denied " + str(decision.get("purpose")))

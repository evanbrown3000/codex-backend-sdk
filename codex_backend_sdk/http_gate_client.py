"""Client for the custody-owned provider HTTP admission socket."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys


def require(method: str, url: str) -> None:
    path = os.environ.get("COGNILODE_HTTP_GATE_SOCKET", "")
    if not path:
        return
    source = os.environ.get("COGNILODE_HTTP_SOURCE") or Path(sys.argv[0] or "python").name
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(60)
            connection.connect(path)
            connection.sendall(
                (
                    json.dumps(
                        {"method": method.upper(), "url": url, "source": source},
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            )
            data = bytearray()
            while b"\n" not in data and len(data) < 65536:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
        value = json.loads(bytes(data).split(b"\n", 1)[0])
    except Exception as exc:
        raise PermissionError("central HTTP gate unavailable") from exc
    if value.get("ok") is not True or value.get("allowed") is not True:
        raise PermissionError("central HTTP gate denied " + str(value.get("purpose") or "request"))


__all__ = ["require"]

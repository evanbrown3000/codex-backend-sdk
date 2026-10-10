"""Thin client for the Python second-order provider scheduler."""
from __future__ import annotations
import json, os, socket, time
from typing import Any, Mapping

DEFAULT_SOCKET = "/runtime/provider-scheduler.sock"

def scheduler_call(payload: Mapping[str, Any], *, timeout: float = 120) -> dict[str, Any]:
    path=os.environ.get("COGNILODE_PROVIDER_SCHEDULER_SOCKET",DEFAULT_SOCKET)
    deadline=time.monotonic()+timeout
    delay=0.1
    while True:
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
                conn.settimeout(max(0.1,deadline-time.monotonic())); conn.connect(path)
                conn.sendall((json.dumps(dict(payload),separators=(",",":"))+"\n").encode())
                conn.shutdown(socket.SHUT_WR)
                chunks=[]
                while True:
                    chunk=conn.recv(65536)
                    if not chunk: break
                    chunks.append(chunk)
            break
        except (FileNotFoundError,ConnectionRefusedError,ConnectionResetError) as exc:
            remaining=deadline-time.monotonic()
            if remaining <= 0:
                raise RuntimeError("central provider scheduler is unavailable") from exc
            time.sleep(min(delay,remaining)); delay=min(2.0,delay*2)
    if not chunks: raise RuntimeError("provider scheduler returned no result")
    result=json.loads(b"".join(chunks).decode())
    if not isinstance(result,dict): raise RuntimeError("provider scheduler returned malformed result")
    if result.get("ok") is False: raise RuntimeError(str(result.get("error") or "provider scheduler rejected operation"))
    return result

def scheduler_available() -> bool:
    return os.path.exists(os.environ.get("COGNILODE_PROVIDER_SCHEDULER_SOCKET",DEFAULT_SOCKET))

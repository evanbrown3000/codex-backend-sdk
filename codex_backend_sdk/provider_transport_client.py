"""Central, fenced provider transport client.

This module is the SDK-side indirection between a durable prompt queue and the
credential-bearing B4PT0R runtime selected by Cognilode's existing relay.  It
observes only the relay-owned operation record; it never reads provider
conversation indexes or histories.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

NAMESPACE = uuid.UUID("81e986cf-4a1b-4b42-98bc-56ee1d43766e")
DEFAULT_BASE = "https://cognilode.com"


def _token() -> str:
    direct = (
        os.environ.get("COGNILODE_OPERATOR_TOKEN", "")
        or os.environ.get("COGNILODE_CENTRAL_ACCESS_TOKEN", "")
    ).strip()
    if direct:
        return re.sub(r"^Bearer\s+", "", direct, flags=re.IGNORECASE).strip().strip('"\'')
    descriptor = os.environ.get("COGNILODE_OPERATOR_TOKEN_FILE", "").strip()
    if not descriptor:
        return ""
    try:
        value = Path(descriptor).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return re.sub(r"^Bearer\s+", "", value, flags=re.IGNORECASE).strip().strip('"\'')


class OperatorTransport:
    def __init__(self, base: str | None = None, timeout: int = 30):
        self.base = (base or os.environ.get("COGNILODE_OPERATOR_URL") or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    def request(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "accept": "application/json,text/plain,*/*",
            "content-type": "application/json",
            "origin": self.base,
            "referer": self.base + "/",
            "user-agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            ),
        }
        token = _token()
        if token:
            headers["authorization"] = f"Bearer {token}"
        request = Request(
            self.base + "/api/operator/provider-transport",
            data=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                failure = json.loads(exc.read(4096))
            except (ValueError, UnicodeDecodeError):
                failure = {}
            code = str(failure.get("error_code") or failure.get("error") or "http_error")[:80]
            raise RuntimeError(f"central_http_{exc.code}:{code}") from exc
        value = json.loads(raw or b"{}")
        if not isinstance(value, dict):
            raise RuntimeError("central_provider_transport_non_object")
        if value.get("ok") is False:
            raise RuntimeError("central_provider_transport:" + str(value.get("error") or "rejected")[:120])
        return value


def _option(args: list[str], name: str, default: str = "") -> str:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return default


def _operation_identity(verb: str, args: list[str], fence: dict[str, Any] | None) -> str:
    if verb != "send":
        return str(uuid.uuid4())
    job_id = _option(args, "--queue-job-id")
    if not isinstance(fence, dict) or not job_id or fence.get("job_id") != job_id:
        raise ValueError("queue_send_fence_missing")
    attempt = str(fence.get("provider_attempt", _option(args, "--provider-attempt", "0")))
    generation = str(fence.get("lease_generation") or "")
    return str(uuid.uuid5(NAMESPACE, f"chatgpt-send:{job_id}:{generation}:{attempt}"))


def transport(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cognilode-unified-provider transport")
    parser.add_argument("--provider", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    args = options.args[1:] if options.args[:1] == ["--"] else options.args
    if options.provider != "chatgpt.com" or len(args) < 3:
        parser.error("the central B4PT0R ChatGPT.com route is required")
    if args[0] != "--auth-source" or args[1] not in {"codex", "chrome"}:
        parser.error("the centrally held provider custody route is required")
    verb = args[2]
    if verb not in {"send", "reconcile", "health"}:
        parser.error("unsupported provider transport operation")

    fence: dict[str, Any] | None = None
    if verb == "send":
        try:
            candidate = json.loads(os.environ.get("COGNILODE_CHATMODE_SEND_FENCE", ""))
            fence = candidate if isinstance(candidate, dict) else None
            operation_id = _operation_identity(verb, args, fence)
        except (ValueError, json.JSONDecodeError):
            print(json.dumps({"state": "queue_send_fence_missing", "provider_acceptance_observed": False}))
            return 64
    else:
        operation_id = _operation_identity(verb, args, fence)

    payload: dict[str, Any] = {
        "operation": "start",
        "operation_id": operation_id,
        "provider": "chatgpt.com",
        "args": args,
    }
    if fence is not None:
        payload["send_fence"] = fence
    selected = os.environ.get("COGNILODE_CHATMODE_NODE_ID", "").strip()
    if selected:
        payload["node_id"] = selected
    operator = OperatorTransport()
    try:
        begun = operator.request(payload)
    except Exception as exc:
        print(json.dumps({
            "state": "central_route_ambiguous",
            "operation_id": operation_id,
            "provider_acceptance_observed": False,
            "error": type(exc).__name__ + ":" + str(exc)[:160],
        }))
        return 75

    node_id = str(begun.get("node_id") or "")
    deadline = time.monotonic() + (1950 if verb == "send" else 330 if verb == "reconcile" else 45)
    while time.monotonic() < deadline:
        try:
            state = operator.request({
                "operation": "read",
                "operation_id": operation_id,
                "node_id": node_id,
            })
        except Exception:
            time.sleep(3)
            continue
        if state.get("state") == "done":
            result = state.get("result") or {}
            stdout = str(result.get("stdout") or "")
            stderr = str(result.get("stderr") or "")
            if stdout:
                sys.stdout.write(stdout + ("" if stdout.endswith("\n") else "\n"))
            if stderr:
                sys.stderr.write(stderr)
            return int(result.get("exit_code") or 0)
        if state.get("state") in {"ambiguous", "conflict"}:
            print(json.dumps({
                "state": "central_route_ambiguous",
                "operation_id": operation_id,
                "provider_acceptance_observed": False,
                "error": str(state.get("error") or state.get("state")),
            }))
            return 75
        time.sleep(3)
    print(json.dumps({
        "state": "central_route_ambiguous",
        "operation_id": operation_id,
        "provider_acceptance_observed": False,
        "error": "provider_operation_deadline",
    }))
    return 75


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values[:1] != ["transport"]:
        parser = argparse.ArgumentParser(prog="cognilode-unified-provider")
        parser.add_argument("command", choices=["transport"])
        parser.parse_args(values)
    return transport(values[1:])


if __name__ == "__main__":
    raise SystemExit(main())

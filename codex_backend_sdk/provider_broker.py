"""Credential-custody operation endpoint for the B4PT0R provider client.

The process is launched by the custody owner after it has materialized a
lease-scoped ``CODEX_HOME`` and connected the mandatory HTTP gate.  Callers
send one JSON operation on stdin and receive one JSON result on stdout; no
provider credential is returned across this boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import OpenAI
from .agent_memory import AgentMemoryClient


def _operator_token() -> str:
    path = Path(
        os.environ.get(
            "COGNILODE_OPERATOR_TOKEN_FILE",
            "/home/worker/.config/cognilode/operator-bearer",
        )
    )
    return path.read_text(encoding="utf-8").strip().removeprefix("Bearer ").strip()


def _relay_request(url: str, method: str, body: Any = None, params: Any = None) -> Any:
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Accept": "application/json", "Authorization": "Bearer " + _operator_token()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=180) as response:
        return json.load(response)


def _artifact_rows(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = turn.get("artifacts")
    return [dict(row) for row in value] if isinstance(value, list) else []


def _send(payload: Mapping[str, Any]) -> dict[str, Any]:
    client = OpenAI().authenticate()
    attachments = [str(value) for value in payload.get("attachments") or []]
    prompt = str(payload.get("prompt") or "")
    with tempfile.TemporaryDirectory(prefix="b4pt0r-operation-") as directory:
        root = Path(directory)
        if payload.get("rendered_conversation"):
            rendered = root / "conversation.md"
            rendered.write_text(str(payload["rendered_conversation"]), encoding="utf-8")
            attachments.insert(0, str(rendered))
            prompt = "First, read the attached conversation.md in full, then " + prompt
        result = client.chatgpt.operations.send(
            prompt,
            conversation_id=payload.get("conversation_id"),
            parent_message_id=payload.get("parent_message_id"),
            model=str(payload.get("model") or "gpt-5-6-thinking"),
            effort=str(payload.get("effort") or "xhigh"),
            attachment_paths=attachments,
            user_message_id=payload.get("user_message_id"),
            readback=True,
            artifact_directory=payload.get("artifact_directory") or root / "artifacts",
        )
        turn = result.get("turn") if isinstance(result.get("turn"), Mapping) else {}
        observation = {
            "conversation_id": result.get("conversation_id"),
            "user_message_id": result.get("user_message_id"),
            "turn": dict(turn),
        }
        try:
            memory = AgentMemoryClient().ingest_chatgpt_turn(observation)
        except Exception as exc:
            memory = {"ingested": False, "error": type(exc).__name__, "message": str(exc)}
        return {
            "ok": True,
            "conversation_id": result.get("conversation_id"),
            "user_message_id": result.get("user_message_id"),
            "assistant_message_id": turn.get("assistant_message_id"),
            "assistant_text": turn.get("assistant_text") or "",
            "provider_receipt": turn.get("model_receipt") or result.get("stream") or {},
            "artifacts": _artifact_rows(turn),
            "agent_memory": memory,
        }


def execute(payload: Mapping[str, Any]) -> Any:
    operation = str(payload.get("operation") or "")
    if operation == "agent_memory_request":
        explicit_url = str(payload.get("url") or "").strip()
        base = os.environ.get(
            "AGENT_MEMORY_ENDPOINT", "https://cognilode.com/api/operator/agent-memory"
        ).rstrip("/")
        return _relay_request(
            explicit_url or (base + "/" + str(payload.get("path") or "").lstrip("/")),
            str(payload.get("method") or "GET"),
            payload.get("body"),
            payload.get("params"),
        )
    if operation == "remote_shell_request":
        body = {
            "jsonrpc": "2.0",
            "id": "b4pt0r-broker",
            "method": "tools/call",
            "params": {
                "name": payload.get("name"),
                "arguments": payload.get("arguments") or {},
            },
        }
        raw = _relay_request(
            os.environ.get(
                "COGNILODE_REMOTE_SHELL_ENDPOINT",
                "https://cognilode.com/api/operator/remote-shell/mcp",
            ),
            "POST",
            body,
        )
        result = raw.get("result", raw) if isinstance(raw, Mapping) else raw
        if isinstance(result, Mapping) and result.get("structuredContent") is not None:
            return result["structuredContent"]
        return result
    if operation == "chatgpt_continue":
        return _send(payload)
    if operation == "conversation_continue":
        if payload.get("context_delivery") != "rendered_conversation_attachment":
            return {"ok": False, "error": "conversation continuation requires rendered attachment"}
        if not payload.get("rendered_conversation"):
            memory = AgentMemoryClient()
            payload = dict(payload)
            payload["rendered_conversation"] = memory.render_markdown(
                str(payload.get("source_conversation_id") or "")
            )
        return _send(payload)
    return {"ok": False, "error": "unsupported broker operation: " + operation}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.readline())
        result = execute(payload)
    except Exception as exc:
        result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if not isinstance(result, Mapping) or result.get("ok") is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())

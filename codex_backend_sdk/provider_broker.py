"""Credential-custody operation endpoint for the B4PT0R provider client.

The process is launched by the custody owner after it has materialized a
lease-scoped ``CODEX_HOME`` and connected the mandatory HTTP gate.  Callers
send one JSON operation on stdin and receive one JSON result on stdout; no
provider credential is returned across this boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import OpenAI
from .agent_memory import AgentMemoryClient
from .attachment_custody import commit_returned_artifact


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
    rows = [dict(row) for row in value] if isinstance(value, list) else []
    for row in rows:
        path = Path(str(row.get("path") or ""))
        if row.get("ref") or not path.is_file():
            continue
        try:
            receipt = commit_returned_artifact(path)
        except Exception as exc:
            row["custody_state"] = "pending"
            row["custody_error"] = type(exc).__name__
        else:
            row.update(receipt)
            row["custody_state"] = "committed"
    return rows


def _send(payload: Mapping[str, Any]) -> dict[str, Any]:
    client = OpenAI().authenticate()
    attachments = [str(value) for value in payload.get("attachments") or []]
    prompt = str(payload.get("prompt") or "")
    memory = AgentMemoryClient(use_broker=False)
    foreground: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="b4pt0r-operation-") as directory:
        root = Path(directory)
        memory_enabled = str(payload.get("memory_context", "required")).casefold() not in {
            "0", "false", "none", "off", "disabled",
        }
        if memory_enabled:
            foreground = memory.prompt_foreground(
                persona=str(payload.get("memory_persona") or "company"),
                max_tokens=int(payload.get("memory_max_tokens") or 40_000),
            )
            foreground_path = root / "foreground.md"
            foreground_path.write_text(str(foreground["context"]), encoding="utf-8")
            actual = hashlib.sha256(foreground_path.read_bytes()).hexdigest()
            if actual != str(foreground.get("content_sha256") or ""):
                raise RuntimeError("Agent Memory foreground content identity mismatch")
            attachments.insert(0, str(foreground_path))
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
        artifact_rows = _artifact_rows(turn)
        try:
            memory_receipt = memory.record_chatgpt_turn(
                conversation_id=str(result.get("conversation_id") or ""),
                user_message_id=str(result.get("user_message_id") or ""),
                assistant_message_id=str(turn.get("assistant_message_id") or ""),
                prompt=prompt,
                response=str(turn.get("assistant_text") or ""),
                attachments=[dict(row) for row in result.get("attachments") or []],
                artifacts=artifact_rows,
                provider_receipt=turn.get("model_receipt") or result.get("stream") or {},
            )
        except Exception as exc:
            memory_receipt = {"ingested": False, "error": type(exc).__name__, "message": str(exc)}
        summary_admission: dict[str, Any] | None = None
        summary_job_ids = re.findall(r"(?m)^- Summary job:\s*(\S+)\s*$", prompt)
        assistant_text = str(turn.get("assistant_text") or "").strip()
        summary_attestation = "read_every_source_fragment_in_full_without_skipping"
        if (
            len(summary_job_ids) == 1
            and assistant_text
            and summary_attestation in assistant_text
        ):
            try:
                admitted = memory.admit_summary_result(
                    job_id=summary_job_ids[0],
                    summary=assistant_text,
                    worker_conversation_url=(
                        "https://chatgpt.com/c/" + str(result.get("conversation_id") or "")
                    ),
                    source_ref=(
                        "b4pt0r-provider-terminal:"
                        + str(result.get("user_message_id") or summary_job_ids[0])
                    ),
                )
                summary_admission = {"ok": True, "result": admitted}
            except Exception as exc:
                summary_admission = {
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "job_id": summary_job_ids[0],
                }
        return {
            "ok": True,
            "conversation_id": result.get("conversation_id"),
            "user_message_id": result.get("user_message_id"),
            "assistant_message_id": turn.get("assistant_message_id"),
            "assistant_text": turn.get("assistant_text") or "",
            "provider_receipt": turn.get("model_receipt") or result.get("stream") or {},
            "artifacts": artifact_rows,
            "agent_memory": memory_receipt,
            "summary_admission": summary_admission,
            "memory_foreground": {
                key: foreground.get(key)
                for key in ("persona_id", "content_sha256", "selected_tokens", "foreground_handle", "selected")
                if foreground.get(key) is not None
            },
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
            memory = AgentMemoryClient(use_broker=False)
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

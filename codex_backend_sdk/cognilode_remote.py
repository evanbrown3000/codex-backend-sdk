"""Cognilode remote-environment ChatGPT product HTTP client.

This module keeps ChatGPT account credentials inside an installed remote environment:
it reads the centrally custodied Codex/ChatGPT OAuth record using the machine credential
grant, gives that record to codex-backend-sdk for refresh/use, writes any refreshed
record back to central custody, and performs ChatGPT product operations from the remote
environment's own network egress.

Large provider payloads are written to caller-selected files. Stdout remains a compact
machine summary so agent-visible shell results stay bounded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
import uuid
import urllib.error
import urllib.parse
import urllib.request

from ._client import CodexClient
from ._streaming import loads_sse_data

DEFAULT_CONTROL_ORIGIN = "https://cognilode.com"
DEFAULT_CREDENTIAL_ID = "chatgpt.codex-auth"
DEFAULT_VISIBLE_CHAR_LIMIT = 3600
DEFAULT_ADMISSION_ORIGIN = "https://cognilode.com"
DEFAULT_DOWNSTREAM_DIR = Path("/var/lib/cognilode/taskflow/terminal-returns")


def _grant_file() -> Path:
    explicit = os.environ.get("COGNILODE_CREDENTIAL_GRANT_FILE", "").strip()
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        Path("/etc/cognilode/credential-grant"),
        Path("/etc/cognilode/github-credential-grant"),
        Path("/workspace/.cognilode/remote-environment/credential-grant"),
        Path("/workspace/.cognilode/remote-environment/github-credential-grant"),
    ]
    for path in candidates:
        if path is not None and path.is_file():
            return path
    raise RuntimeError("no Cognilode machine credential grant is available")


def _control_json(path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    grant = _grant_file().read_text(encoding="utf-8").strip()
    if not grant:
        raise RuntimeError("Cognilode machine credential grant is empty")
    origin = os.environ.get("COGNILODE_CONTROL_ORIGIN", DEFAULT_CONTROL_ORIGIN).rstrip("/")
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        origin + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + grant,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Cognilode-B4PT0R-Remote/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("Cognilode control response is not an object")
    return value


def _read_auth_record(credential_id: str) -> dict[str, Any]:
    path = "/api/operator/credentials/" + urllib.parse.quote(credential_id, safe="")
    payload = _control_json(path)
    value = payload.get("value")
    if not isinstance(value, dict):
        raise RuntimeError("centrally custodied ChatGPT auth record is not an object")
    tokens = value.get("tokens")
    if not isinstance(tokens, dict) or not isinstance(tokens.get("refresh_token"), str):
        raise RuntimeError("centrally custodied ChatGPT auth record lacks Codex OAuth tokens")
    return value


def _write_auth_record(credential_id: str, value: dict[str, Any]) -> None:
    environment = (
        os.environ.get("COGNILODE_REMOTE_ENVIRONMENT_ID")
        or os.environ.get("COGNILODE_ENVIRONMENT_ID")
        or "remote-environment"
    )
    payload = _control_json(
        "/api/operator/credentials",
        method="POST",
        body={
            "id": credential_id,
            "value": value,
            "source": "remote-environment:" + environment,
            "label": "ChatGPT/Codex OAuth account authentication",
            "tags": ["chatgpt", "codex", "account-auth", "remote-http"],
        },
    )
    if payload.get("ok") is False:
        raise RuntimeError("refreshed ChatGPT auth could not be returned to central custody")


class RemoteChatGPT:
    def __init__(self, *, credential_id: str = DEFAULT_CREDENTIAL_ID, timeout: float = 60.0):
        self.credential_id = credential_id
        self.timeout = timeout
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self.client: CodexClient | None = None

    def __enter__(self) -> CodexClient:
        try:
            auth = _read_auth_record(self.credential_id)
        except RuntimeError as exc:
            if "credential grant" not in str(exc).lower():
                raise
            client = CodexClient(timeout=self.timeout, max_retries=0).authenticate(interactive=False)
            self.client = client
            return client
        self._tmp = tempfile.TemporaryDirectory(prefix="cognilode-b4pt0r-")
        home = Path(self._tmp.name)
        auth_path = home / "auth.json"
        auth_path.write_text(json.dumps(auth, separators=(",", ":")), encoding="utf-8")
        os.chmod(auth_path, 0o600)
        previous = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            client = CodexClient(timeout=self.timeout, max_retries=0).authenticate(interactive=False)
            refreshed = json.loads(auth_path.read_text(encoding="utf-8"))
            if isinstance(refreshed, dict):
                _write_auth_record(self.credential_id, refreshed)
            self.client = client
            return client
        except Exception:
            if previous is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous
            self._tmp.cleanup()
            self._tmp = None
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.client is not None:
            self.client.close()
        if self._tmp is not None:
            self._tmp.cleanup()


def _write_json(path: str | None, value: Any) -> None:
    if not path:
        return
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _bounded_summary(value: Any, *, limit: int = DEFAULT_VISIBLE_CHAR_LIMIT) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"...<provider payload omitted from stdout; {omitted} chars remain in output file>"


def _consume_stream(response: Any, output_path: str | None) -> dict[str, Any]:
    target = Path(output_path).expanduser() if output_path else None
    handle = None
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = target.open("wb")
    line_count = 0
    byte_count = 0
    last_line = ""
    try:
        for raw in response.iter_lines(decode_unicode=False):
            if raw is None:
                continue
            line = bytes(raw)
            byte_count += len(line) + 1
            line_count += 1
            if handle is not None:
                handle.write(line + b"\n")
            try:
                last_line = line.decode("utf-8", errors="replace")
            except Exception:
                last_line = ""
    finally:
        if handle is not None:
            handle.close()
        response.close()
    return {
        "ok": True,
        "status": getattr(response, "status_code", None),
        "stream_lines": line_count,
        "stream_bytes": byte_count,
        "output": str(target) if target is not None else None,
        "last_event_excerpt": last_line[-1200:],
    }


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if isinstance(parts, list):
        texts = []
        for part in parts:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        if texts:
            return "\n".join(texts).strip()
    text = content.get("text")
    return text.strip() if isinstance(text, str) else ""


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _capture_stream(response: Any, output_path: str) -> dict[str, Any]:
    target = Path(output_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    line_count = 0
    byte_count = 0
    data_lines: list[str] = []
    text_chunks: list[str] = []
    assistant_text = ""
    conversation_id = None
    assistant_message_id = None
    terminal = False
    try:
        with target.open("wb") as handle:
            for raw in response.iter_lines(decode_unicode=False):
                if raw is None:
                    continue
                line = bytes(raw)
                handle.write(line + b"\n")
                line_count += 1
                byte_count += len(line) + 1
                decoded = line.decode("utf-8", errors="replace")
                if decoded.startswith("data:"):
                    data_lines.append(decoded[len("data:"):].strip())
                    continue
                if decoded == "" and data_lines:
                    payload = loads_sse_data(data_lines)
                    data_lines = []
                    if payload is None:
                        continue
                    for item in _walk_dicts(payload):
                        conversation_id = conversation_id or item.get("conversation_id") or item.get("conversationId")
                        message = item.get("message") if isinstance(item.get("message"), dict) else item
                        author = message.get("author") if isinstance(message, dict) else None
                        role = author.get("role") if isinstance(author, dict) else message.get("role")
                        if role == "assistant":
                            assistant_message_id = (
                                assistant_message_id
                                or message.get("id")
                                or item.get("message_id")
                                or item.get("messageId")
                            )
                            current = _message_text(message)
                            if current:
                                assistant_text = current
                            terminal = terminal or bool(message.get("end_turn") is True)
                        delta = item.get("delta")
                        if isinstance(delta, str):
                            text_chunks.append(delta)
                        text = item.get("text")
                        if isinstance(text, str) and item.get("type") in {
                            "message_delta",
                            "text_delta",
                            "response.output_text.delta",
                        }:
                            text_chunks.append(text)
    finally:
        response.close()
    text = assistant_text or "".join(text_chunks).strip()
    return {
        "ok": bool(text),
        "status": getattr(response, "status_code", None),
        "stream_lines": line_count,
        "stream_bytes": byte_count,
        "output": str(target),
        "terminal": terminal or bool(text),
        "text": text,
        "conversation_id": conversation_id,
        "assistant_message_id": assistant_message_id,
    }


def _body_file(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("conversation request body must be a JSON object")
    return value


def _read_text_arg(value: str | None, path: str | None) -> str:
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    if value:
        return value
    raise RuntimeError("prompt text is required")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_prompt_body(
    prompt: str,
    *,
    model: str,
    effort: str | None,
    conversation_id: str | None,
    parent_message_id: str | None,
) -> dict[str, Any]:
    message_id = str(uuid.uuid4())
    body: dict[str, Any] = {
        "action": "next",
        "messages": [
            {
                "id": message_id,
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt]},
                "metadata": {},
            }
        ],
        "parent_message_id": parent_message_id or str(uuid.uuid4()),
        "model": model,
        "timezone_offset_min": int(os.environ.get("COGNILODE_CHATGPT_TIMEZONE_OFFSET_MIN", "300")),
        "suggestions": [],
        "history_and_training_disabled": False,
        "conversation_mode": {"kind": "primary_assistant"},
        "force_paragen": False,
        "force_rate_limit": False,
        "websocket_request_id": str(uuid.uuid4()),
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    if effort:
        body["thinking_effort"] = effort
    return body


def _git_marker(path: str) -> str:
    import subprocess

    try:
        head = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = subprocess.run(
            ["git", "-C", path, "diff", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0
        return f"{head}{'-dirty' if dirty else ''}"
    except Exception:
        return "unknown"


def _auth_token() -> str:
    direct = os.environ.get("COGNILODE_OPERATOR_TOKEN", "").strip()
    if direct:
        return direct
    candidates = [
        os.environ.get("COGNILODE_OPERATOR_TOKEN_FILE", ""),
        "/opt/cognilode/runtime/control-secrets/operator-token",
        "/etc/cognilode/remote-shell-grant",
        str(Path.home() / ".config/cognilode/operator-token"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            token = Path(candidate).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            return token
    return ""


def _admit_memory(record: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    token = _auth_token()
    if not token:
        return False, {"error": "agent_memory_auth_unavailable"}
    origin = os.environ.get("COGNILODE_ADMISSION_ORIGIN", DEFAULT_ADMISSION_ORIGIN).rstrip("/")
    request = urllib.request.Request(
        f"{origin}/api/operator/agent-memory",
        data=json.dumps(record, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Cognilode-B4PT0R-ChatMode/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=55) as response:
            value = json.loads(response.read().decode("utf-8") or "{}")
        return bool(value.get("ok")), value if isinstance(value, dict) else {"response": value}
    except urllib.error.HTTPError as exc:
        return False, {"error": f"http_{exc.code}", "body": exc.read(1000).decode(errors="replace")}
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}:{str(exc)[:400]}"}


def _record_terminal_return(
    *,
    prompt: str,
    capture: dict[str, Any],
    output_dir: str | None,
    model: str,
    effort: str | None,
    source_path: str,
    admit: bool,
) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record_id = f"b4pt0r-chatmode-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    record = {
        "schema": "cognilode.taskflow.terminal_return.v1",
        "operation": "record_conversation",
        "record_id": record_id,
        "capability": "B4PT0R Chat-mode Prompt Dispatch",
        "provider": "chatgpt.com",
        "agent_id": "chatgpt.web.default",
        "model_requested": model,
        "reasoning_effort_requested": effort,
        "prompt": prompt,
        "response_text": capture.get("text") or "",
        "response_excerpt": (capture.get("text") or "")[:12000],
        "conversation_id": capture.get("conversation_id"),
        "assistant_message_id": capture.get("assistant_message_id"),
        "prompt_sha256": _sha(prompt),
        "response_sha256": _sha(capture.get("text") or "") if capture.get("text") else None,
        "source_code_path": source_path,
        "source_code_commit": _git_marker(source_path),
        "timestamp": now,
        "semantic_acceptance_pending": True,
        "raw_sse_path": capture.get("output"),
    }
    root = Path(output_dir).expanduser() if output_dir else DEFAULT_DOWNSTREAM_DIR
    try:
        root.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        root = Path.home() / ".local/share/cognilode/taskflow/terminal-returns"
        root.mkdir(parents=True, exist_ok=True)
    record_path = root / f"{record_id}.json"
    _write_json(str(record_path), record)
    memory_ok = False
    memory_receipt: dict[str, Any] = {"skipped": True}
    if admit:
        memory_ok, memory_receipt = _admit_memory(record)
    return {
        "record_id": record_id,
        "record_path": str(record_path),
        "memory_admitted": memory_ok,
        "memory_receipt": memory_receipt,
        "record": record,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cognilode-chatgpt-http")
    parser.add_argument("--credential-id", default=DEFAULT_CREDENTIAL_ID)
    parser.add_argument("--timeout", type=float, default=60.0)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("conversation-list")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--order", default="updated")
    p.add_argument("--output")

    p = sub.add_parser("conversation-read")
    p.add_argument("conversation_id")
    p.add_argument("--output", required=True)

    p = sub.add_parser("conversation-search")
    p.add_argument("query")
    p.add_argument("--output", required=True)

    p = sub.add_parser("conversation-create")
    p.add_argument("--body-file", required=True)
    p.add_argument("--output", required=True)

    p = sub.add_parser("conversation-resume")
    p.add_argument("--body-file", required=True)
    p.add_argument("--output", required=True)

    p = sub.add_parser("prompt-send")
    p.add_argument("--prompt")
    p.add_argument("--prompt-file")
    p.add_argument("--conversation-id")
    p.add_argument("--parent-message-id")
    p.add_argument("--model", default=os.environ.get("COGNILODE_CHATGPT_MODEL", "gpt-5.5"))
    p.add_argument("--effort", default=os.environ.get("COGNILODE_CHATGPT_EFFORT", "xhigh"))
    p.add_argument("--output", required=True)
    p.add_argument("--record-dir")
    p.add_argument("--no-admit", action="store_true")

    p = sub.add_parser("library-list")
    p.add_argument("--output", required=True)

    p = sub.add_parser("library-nodes")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--cursor")
    p.add_argument("--output", required=True)

    p = sub.add_parser("library-download")
    p.add_argument("file_id")
    p.add_argument("output_path")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with RemoteChatGPT(credential_id=args.credential_id, timeout=args.timeout) as client:
        if args.command == "conversation-list":
            value = client.chatgpt.conversations.list(limit=args.limit, order=args.order)
            _write_json(args.output, value)
            print(_bounded_summary({"ok": True, "output": args.output, "items": len(value.get("items") or [])}))
            return 0
        if args.command == "conversation-read":
            value = client.chatgpt.conversations.retrieve(args.conversation_id)
            _write_json(args.output, value)
            print(_bounded_summary({"ok": True, "conversation_id": args.conversation_id, "output": args.output}))
            return 0
        if args.command == "conversation-search":
            value = client.chatgpt.conversations.search(args.query)
            _write_json(args.output, value)
            print(_bounded_summary({"ok": True, "output": args.output}))
            return 0
        if args.command == "conversation-create":
            response = client.chatgpt.conversations.create_stream(_body_file(args.body_file))
            print(_bounded_summary(_consume_stream(response, args.output)))
            return 0
        if args.command == "conversation-resume":
            response = client.chatgpt.conversations.resume_stream(_body_file(args.body_file))
            print(_bounded_summary(_consume_stream(response, args.output)))
            return 0
        if args.command == "prompt-send":
            prompt = _read_text_arg(args.prompt, args.prompt_file)
            body = _make_prompt_body(
                prompt,
                model=args.model,
                effort=args.effort,
                conversation_id=args.conversation_id,
                parent_message_id=args.parent_message_id,
            )
            response = client.chatgpt.conversations.create_stream(body)
            capture = _capture_stream(response, args.output)
            admission = _record_terminal_return(
                prompt=prompt,
                capture=capture,
                output_dir=args.record_dir,
                model=args.model,
                effort=args.effort,
                source_path=str(Path(__file__).resolve().parents[1]),
                admit=not args.no_admit,
            )
            print(_bounded_summary({"ok": bool(capture.get("text")), **capture, **admission}))
            return 0
        if args.command == "library-list":
            value = client.chatgpt.files.list_library_files({})
            _write_json(args.output, value)
            print(_bounded_summary({"ok": True, "output": args.output}))
            return 0
        if args.command == "library-nodes":
            kwargs: dict[str, Any] = {"limit": args.limit}
            if args.cursor:
                kwargs["cursor"] = args.cursor
            value = client.chatgpt.files.list_library_nodes(**kwargs)
            _write_json(args.output, value)
            print(_bounded_summary({"ok": True, "output": args.output}))
            return 0
        if args.command == "library-download":
            target = str(Path(args.output_path).expanduser())
            client.chatgpt.files.download(args.file_id, response_format="file", output_path=target)
            size = Path(target).stat().st_size
            print(_bounded_summary({"ok": True, "file_id": args.file_id, "output": target, "bytes": size}))
            return 0
    raise RuntimeError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())

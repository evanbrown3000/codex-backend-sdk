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
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import urllib.parse
import urllib.request

from ._client import CodexClient

DEFAULT_CONTROL_ORIGIN = "https://cognilode.com"
DEFAULT_CREDENTIAL_ID = "chatgpt.codex-auth"
DEFAULT_VISIBLE_CHAR_LIMIT = 3600


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
        auth = _read_auth_record(self.credential_id)
        self._tmp = tempfile.TemporaryDirectory(prefix="cognilode-b4pt0r-")
        home = Path(self._tmp.name)
        auth_path = home / "auth.json"
        auth_path.write_text(json.dumps(auth, separators=(",", ":")), encoding="utf-8")
        os.chmod(auth_path, 0o600)
        previous = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            client = CodexClient(timeout=self.timeout, max_retries=1).authenticate(interactive=False)
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


def _body_file(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("conversation request body must be a JSON object")
    return value


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

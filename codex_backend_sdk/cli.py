"""Machine-oriented CLI over B4PT0R SDK, remote shell, and Agent Memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

from . import OpenAI
from .agent_memory import AgentMemoryClient
from .remote_shell import RemoteShellClient
from .storage import load_tokens, token_needs_refresh


def _emit(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def _prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return args.prompt
    if args.prompt_file is not None:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("Provide --prompt, --prompt-file, or prompt text on stdin.")


def _client():
    return OpenAI().authenticate()


def _chatgpt(args: argparse.Namespace) -> Any:
    client = _client()
    if args.chatgpt_command == "list":
        return client.chatgpt.conversations.list(
            offset=args.offset, limit=args.limit, order=args.order
        )
    if args.chatgpt_command == "search":
        return client.chatgpt.conversations.search(args.query)
    if args.chatgpt_command == "read":
        conversation = client.chatgpt.conversations.retrieve(args.conversation_id)
        result: dict[str, Any] = {"conversation": conversation}
        if not args.no_memory_ingest:
            try:
                result["agent_memory"] = AgentMemoryClient().ingest_chatgpt_conversation(
                    conversation
                )
            except Exception as error:
                result["agent_memory"] = {
                    "ingested": False,
                    "error": type(error).__name__,
                    "message": str(error),
                }
        return result
    if args.chatgpt_command == "upload":
        return client.chatgpt.operations.upload_attachments(args.path)
    if args.chatgpt_command == "send":
        result = client.chatgpt.operations.send(
            _prompt(args),
            conversation_id=args.conversation_id,
            parent_message_id=args.parent_message_id,
            model=args.model,
            effort=args.effort,
            attachment_paths=args.attach,
            connector_ids=args.connector,
            user_message_id=args.user_message_id,
            readback=not args.no_readback,
            artifact_directory=args.artifact_dir,
        )
        if not args.no_memory_ingest:
            try:
                result["agent_memory"] = AgentMemoryClient().ingest_chatgpt_turn(result)
            except Exception as error:
                result["agent_memory"] = {
                    "ingested": False,
                    "error": type(error).__name__,
                    "message": str(error),
                }
        return result
    if args.chatgpt_command == "collect":
        return client.chatgpt.operations.collect(
            args.conversation_id,
            args.user_message_id,
            artifact_directory=args.artifact_dir,
        )
    raise ValueError(f"Unknown ChatGPT command: {args.chatgpt_command}")


def _remote(args: argparse.Namespace) -> Any:
    client = RemoteShellClient(actor_id=args.actor_id)
    if args.remote_command == "list":
        return client.environments()
    if args.remote_command == "select":
        return client.select(args.environment_id)
    if args.remote_command == "exec":
        return client.execute(
            args.command,
            workdir=args.workdir,
            environment_id=args.environment_id,
            yield_time_ms=args.yield_time_ms,
        )
    if args.remote_command == "write":
        return client.write(
            args.process_id,
            chars=args.chars,
            environment_id=args.environment_id,
            yield_time_ms=args.yield_time_ms,
        )
    raise ValueError(f"Unknown remote command: {args.remote_command}")


def _memory(args: argparse.Namespace) -> Any:
    client = AgentMemoryClient()
    if args.memory_command == "recent":
        return client.recent(limit=args.limit, provider=args.provider)
    if args.memory_command == "search":
        return client.search(args.query, limit=args.limit, provider=args.provider)
    if args.memory_command == "get":
        return client.get(args.conversation_id)
    if args.memory_command == "read":
        return client.read(
            args.conversation_id,
            reader_id=args.reader_id,
            after=args.after,
            full=args.full,
            peek=args.peek,
            projection=args.projection,
        )
    if args.memory_command == "render":
        return {"conversation_id": args.conversation_id, "markdown": client.render_markdown(args.conversation_id)}
    raise ValueError(f"Unknown memory command: {args.memory_command}")


def _auth(args: argparse.Namespace) -> Any:
    del args
    store = load_tokens()
    if store is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "account_id": store.account_id,
        "plan_type": store.plan_type,
        "refresh_required": token_needs_refresh(store),
    }


def _transfer(args: argparse.Namespace) -> Any:
    markdown = AgentMemoryClient().render_markdown(args.source_conversation_id)
    with tempfile.TemporaryDirectory(prefix="b4pt0r-conversation-") as directory:
        source = Path(directory) / "conversation.md"
        source.write_text(markdown, encoding="utf-8")
        followup = _prompt(args).strip()
        prompt = "First, please read the attached conversation.md in full."
        if followup:
            prompt = f"{prompt}\n\n{followup}"
        return _client().chatgpt.operations.send(
            prompt,
            conversation_id=args.destination_conversation_id,
            model=args.model,
            effort=args.effort,
            attachment_paths=[source],
            artifact_directory=args.artifact_dir,
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="b4pt0r", description=__doc__)
    providers = root.add_subparsers(dest="provider", required=True)

    auth = providers.add_parser("auth")
    auth.add_subparsers(dest="auth_command", required=True).add_parser("status")

    chatgpt = providers.add_parser("chatgpt")
    chat = chatgpt.add_subparsers(dest="chatgpt_command", required=True)
    listing = chat.add_parser("list")
    listing.add_argument("--offset", type=int)
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--order")
    searching = chat.add_parser("search")
    searching.add_argument("query")
    reading = chat.add_parser("read")
    reading.add_argument("conversation_id")
    reading.add_argument("--no-memory-ingest", action="store_true")
    upload = chat.add_parser("upload")
    upload.add_argument("path", nargs="+")
    send = chat.add_parser("send")
    send.add_argument("--prompt")
    send.add_argument("--prompt-file")
    send.add_argument("--conversation-id")
    send.add_argument("--parent-message-id")
    send.add_argument("--model", default="gpt-5-6-thinking")
    send.add_argument("--effort", choices=("medium", "high", "xhigh"), default="high")
    send.add_argument("--attach", action="append", default=[])
    send.add_argument("--connector", action="append", default=[])
    send.add_argument("--user-message-id")
    send.add_argument("--artifact-dir")
    send.add_argument("--no-readback", action="store_true")
    send.add_argument("--no-memory-ingest", action="store_true")
    collect = chat.add_parser("collect")
    collect.add_argument("conversation_id")
    collect.add_argument("user_message_id")
    collect.add_argument("--artifact-dir")

    remote = providers.add_parser("remote")
    remote.add_argument("--actor-id", default="default")
    shell = remote.add_subparsers(dest="remote_command", required=True)
    shell.add_parser("list")
    select = shell.add_parser("select")
    select.add_argument("environment_id")
    execute = shell.add_parser("exec")
    execute.add_argument("command")
    execute.add_argument("--workdir")
    execute.add_argument("--environment-id")
    execute.add_argument("--yield-time-ms", type=int, default=9000)
    write = shell.add_parser("write")
    write.add_argument("process_id")
    write.add_argument("--chars", default="")
    write.add_argument("--environment-id")
    write.add_argument("--yield-time-ms", type=int, default=9000)

    memory = providers.add_parser("memory")
    mem = memory.add_subparsers(dest="memory_command", required=True)
    recent = mem.add_parser("recent")
    recent.add_argument("--limit", type=int, default=50)
    recent.add_argument("--provider")
    search = mem.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--provider")
    get = mem.add_parser("get")
    get.add_argument("conversation_id")
    read = mem.add_parser("read")
    read.add_argument("conversation_id")
    read.add_argument("--reader-id", required=True)
    read.add_argument("--after")
    read.add_argument("--full", action="store_true")
    read.add_argument("--peek", action="store_true")
    read.add_argument("--projection", default="read-model")
    render = mem.add_parser("render")
    render.add_argument("conversation_id")

    transfer = providers.add_parser("transfer")
    transfer.add_argument("source_conversation_id")
    transfer.add_argument("--destination-conversation-id")
    transfer.add_argument("--prompt")
    transfer.add_argument("--prompt-file")
    transfer.add_argument("--model", default="gpt-5-6-thinking")
    transfer.add_argument("--effort", choices=("medium", "high", "xhigh"), default="high")
    transfer.add_argument("--artifact-dir")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.provider == "auth":
            result = _auth(args)
        elif args.provider == "chatgpt":
            result = _chatgpt(args)
        elif args.provider == "remote":
            result = _remote(args)
        elif args.provider == "memory":
            result = _memory(args)
        elif args.provider == "transfer":
            result = _transfer(args)
        else:
            raise ValueError(f"Unknown provider: {args.provider}")
        _emit(result)
        return 0
    except Exception as error:
        _emit({"error": type(error).__name__, "message": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

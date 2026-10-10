"""Machine-oriented CLI over B4PT0R SDK, remote shell, and Agent Memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .agent_memory import AgentMemoryClient
from .bridge_provider import ProviderCommandClient
from .remote_shell import RemoteShellClient


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


def _json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    path = Path(value).expanduser()
    raw = path.read_text(encoding="utf-8") if path.is_file() else value
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object or a path containing one")
    return parsed


def _chatgpt(args: argparse.Namespace) -> Any:
    memory = AgentMemoryClient()
    if args.chatgpt_command == "list":
        return memory.list(limit=args.limit, cursor=str(args.offset) if args.offset else None, provider="chatgpt.com")
    if args.chatgpt_command == "search":
        return memory.search(args.query, provider="chatgpt.com")
    if args.chatgpt_command == "read":
        return memory.read(
            args.conversation_id, reader_id=args.reader_id, after=args.after,
            full=args.full, peek=args.peek, projection=args.projection,
        )
    if args.chatgpt_command == "upload":
        provider = ProviderCommandClient()
        return {"attachment_refs": [provider.stage_file(path) for path in args.path]}
    if args.chatgpt_command == "send":
        provider = ProviderCommandClient()
        queued = provider.send_chatgpt(
            prompt=_prompt(args),
            conversation_id=args.conversation_id,
            parent_message_id=args.parent_message_id,
            model=args.model,
            effort=args.effort,
            attachments=args.attach,
            request_id=args.user_message_id,
            project=args.project,
            role=args.role,
            source=args.source,
            prompt_authority=args.prompt_authority,
            priority=args.priority,
            decisionx=_json_object(args.decisionx),
        )
        if args.no_readback:
            return queued
        completed = provider.wait(str(queued["job_id"]))
        conversation_id = str(completed.get("conversation_id") or "")
        return {
            **completed,
            "conversation": memory.get(conversation_id) if completed.get("ok") and conversation_id else None,
        }
    if args.chatgpt_command == "collect":
        return ProviderCommandClient().wait(args.job_id)
    raise ValueError(f"Unknown ChatGPT command: {args.chatgpt_command}")


def _prompting(args: argparse.Namespace) -> Any:
    provider = ProviderCommandClient()
    if args.prompting_command == "send":
        queued = provider.send_provider(
            provider=args.destination,
            prompt=_prompt(args),
            conversation_id=args.conversation_id,
            parent_message_id=args.parent_message_id,
            environment_id=args.environment_id,
            model=args.model,
            effort=args.effort,
            attachments=args.attach,
            request_id=args.request_id,
            project=args.project,
            role=args.role,
            source=args.source,
            prompt_authority=args.prompt_authority,
            priority=args.priority,
            decisionx=_json_object(args.decisionx),
        )
        return queued if args.no_readback else provider.wait(str(queued["job_id"]))
    if args.prompting_command == "collect":
        return provider.wait(args.job_id)
    if args.prompting_command == "queue":
        return provider.queue_status(args.destination)
    memory = AgentMemoryClient()
    if args.prompting_command == "list":
        filters = {"provider": args.destination} if args.destination else {}
        return memory.list(limit=args.limit, cursor=args.cursor, **filters)
    if args.prompting_command == "search":
        filters = {"provider": args.destination} if args.destination else {}
        return memory.search(args.query, limit=args.limit, cursor=args.cursor, **filters)
    if args.prompting_command == "read":
        return memory.read(
            args.conversation_id,
            reader_id=args.reader_id,
            after=args.after,
            full=args.full,
            peek=args.peek,
            projection=args.projection,
        )
    raise ValueError(f"Unknown prompting command: {args.prompting_command}")


def _remote(args: argparse.Namespace) -> Any:
    client = RemoteShellClient(actor_id=args.actor_id)
    if args.remote_command == "list":
        return client.environments()
    if args.remote_command == "select":
        return client.select(args.environment_id)
    if args.remote_command == "current":
        return client.current()
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
        return client.recent(limit=args.limit, provider=args.memory_provider)
    if args.memory_command == "search":
        return client.search(args.query, limit=args.limit, provider=args.memory_provider)
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
    return ProviderCommandClient().queue_status()


def _transfer(args: argparse.Namespace) -> Any:
    markdown = AgentMemoryClient().render_markdown(args.source_conversation_id)
    return ProviderCommandClient().continue_from_memory(
        source_conversation_id=args.source_conversation_id,
        prompt=_prompt(args).strip(),
        destination_provider="chatgpt",
        destination_conversation_id=args.destination_conversation_id,
        model=args.model,
        effort=args.effort,
        rendered_conversation=markdown,
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
    reading.add_argument("--reader-id", default="b4pt0r-cli")
    reading.add_argument("--after")
    reading.add_argument("--full", action="store_true")
    reading.add_argument("--peek", action="store_true")
    reading.add_argument("--projection", default="read-model")
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
    send.add_argument("--user-message-id")
    send.add_argument("--project", default="unified-b4pt0r")
    send.add_argument("--role", default="interactive-operator")
    send.add_argument("--source", default="b4pt0r-unified-cli")
    send.add_argument("--prompt-authority", default="interactive_operator")
    send.add_argument("--priority", type=int)
    send.add_argument("--decisionx", help="DecisionX receipt JSON or file path")
    send.add_argument("--no-readback", action="store_true")
    collect = chat.add_parser("collect")
    collect.add_argument("job_id")

    prompting = providers.add_parser(
        "prompt", help="Queue any supported provider through one interface"
    )
    prompt_ops = prompting.add_subparsers(dest="prompting_command", required=True)
    unified_send = prompt_ops.add_parser("send")
    unified_send.add_argument("--provider", dest="destination", required=True,
                              choices=("chatgpt.com", "gemini.com", "claude.com",
                                       "anthropic.com", "codex.research"))
    unified_send.add_argument("--prompt")
    unified_send.add_argument("--prompt-file")
    unified_send.add_argument("--conversation-id")
    unified_send.add_argument("--parent-message-id")
    unified_send.add_argument("--environment-id")
    unified_send.add_argument("--model")
    unified_send.add_argument("--effort")
    unified_send.add_argument("--attach", action="append", default=[])
    unified_send.add_argument("--request-id")
    unified_send.add_argument("--project", default="unified-b4pt0r")
    unified_send.add_argument("--role", default="interactive-operator")
    unified_send.add_argument("--source", default="b4pt0r-unified-cli")
    unified_send.add_argument("--prompt-authority", default="interactive_operator")
    unified_send.add_argument("--priority", type=int)
    unified_send.add_argument("--decisionx")
    unified_send.add_argument("--no-readback", action="store_true")
    unified_collect = prompt_ops.add_parser("collect")
    unified_collect.add_argument("job_id")
    unified_queue = prompt_ops.add_parser("queue")
    unified_queue.add_argument("--provider", dest="destination", required=True)
    unified_list = prompt_ops.add_parser("list")
    unified_list.add_argument("--provider", dest="destination")
    unified_list.add_argument("--limit", type=int, default=50)
    unified_list.add_argument("--cursor")
    unified_search = prompt_ops.add_parser("search")
    unified_search.add_argument("query")
    unified_search.add_argument("--provider", dest="destination")
    unified_search.add_argument("--limit", type=int, default=50)
    unified_search.add_argument("--cursor")
    unified_read = prompt_ops.add_parser("read")
    unified_read.add_argument("conversation_id")
    unified_read.add_argument("--reader-id", default="b4pt0r-cli")
    unified_read.add_argument("--after")
    unified_read.add_argument("--full", action="store_true")
    unified_read.add_argument("--peek", action="store_true")
    unified_read.add_argument("--projection", default="read-model")

    remote = providers.add_parser("remote")
    remote.add_argument("--actor-id", default="default")
    shell = remote.add_subparsers(dest="remote_command", required=True)
    shell.add_parser("list")
    shell.add_parser("current")
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
    recent.add_argument("--provider", dest="memory_provider")
    search = mem.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--provider", dest="memory_provider")
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
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.provider == "auth":
            result = _auth(args)
        elif args.provider == "chatgpt":
            result = _chatgpt(args)
        elif args.provider == "prompt":
            result = _prompting(args)
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

"""Pass-through Codex App Server bridge with ChatGPT and Agent Memory threads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping
import uuid

from . import OpenAI
from .agent_memory import AgentMemoryClient
from .environment_app_server import SelectedEnvironmentAppServer, _environment_id
from .remote_shell import RemoteShellClient


def _text(message: Mapping[str, Any]) -> str:
    content = message.get("content") if isinstance(message.get("content"), Mapping) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    values: list[str] = []
    for part in parts:
        if isinstance(part, str):
            values.append(part)
        elif isinstance(part, Mapping):
            value = part.get("text") or part.get("content")
            if isinstance(value, str):
                values.append(value)
    return "\n".join(values)


def _branch(conversation: Mapping[str, Any]) -> list[dict[str, Any]]:
    mapping = conversation.get("mapping") if isinstance(conversation.get("mapping"), Mapping) else {}
    current = conversation.get("current_node")
    messages: list[dict[str, Any]] = []
    visited: set[str] = set()
    while isinstance(current, str) and current and current not in visited:
        visited.add(current)
        node = mapping.get(current)
        if not isinstance(node, Mapping):
            break
        message = node.get("message")
        if isinstance(message, dict):
            messages.append(message)
        current = node.get("parent")
    messages.reverse()
    return messages


def _provider_thread(conversation_id: str, conversation: Mapping[str, Any]) -> dict[str, Any]:
    messages = _branch(conversation)
    turns: list[dict[str, Any]] = []
    active_items: list[dict[str, Any]] = []
    active_id: str | None = None
    for message in messages:
        author = message.get("author") if isinstance(message.get("author"), Mapping) else {}
        role = author.get("role")
        message_id = str(message.get("id") or uuid.uuid4())
        if role == "user":
            if active_items:
                turns.append({"id": active_id, "status": "completed", "items": active_items})
            active_id = f"chatgpt-turn:{message_id}"
            active_items = [
                {
                    "id": message_id,
                    "type": "userMessage",
                    "content": [{"type": "text", "text": _text(message)}],
                }
            ]
        elif role == "assistant":
            if not active_items:
                active_id = f"chatgpt-turn:{message_id}"
            active_items.append(
                {"id": message_id, "type": "agentMessage", "text": _text(message)}
            )
            if message.get("end_turn") is True or message.get("status") == "finished_successfully":
                turns.append({"id": active_id, "status": "completed", "items": active_items})
                active_items = []
                active_id = None
    if active_items:
        turns.append({"id": active_id, "status": "inProgress", "items": active_items})
    title = conversation.get("title")
    preview = next(
        (_text(message) for message in messages if _text(message)),
        "ChatGPT conversation",
    )
    updated = max(
        (
            float(message.get("update_time") or message.get("create_time") or 0)
            for message in messages
        ),
        default=0,
    )
    return {
        "id": f"chatgpt:{conversation_id}",
        "name": str(title or preview[:80]),
        "preview": preview[:240],
        "updatedAt": int(updated),
        "cwd": "chatgpt.com",
        "status": {"type": "idle"},
        "section": {"id": "chatgpt", "name": "ChatGPT"},
        "turns": turns,
    }


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("data", "items", "conversations", "results", "documents"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
    return []


def _memory_summary(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if isinstance(row.get("thread"), Mapping):
        return dict(row["thread"])
    raw_id = row.get("conversation_id") or row.get("id") or row.get("thread_id")
    if not isinstance(raw_id, str) or not raw_id:
        return None
    provider = str(row.get("provider") or row.get("platform") or "memory")
    provider_conversation_id = row.get("provider_conversation_id")
    projected_id = (
        f"chatgpt:{provider_conversation_id}"
        if provider.lower() in {"chatgpt", "chatgpt.com"}
        and isinstance(provider_conversation_id, str)
        and provider_conversation_id
        else f"memory:{raw_id}"
    )
    name = row.get("title") or row.get("name") or row.get("subject") or raw_id
    preview = row.get("preview") or row.get("snippet") or row.get("text") or ""
    return {
        "id": projected_id,
        "name": str(name),
        "preview": str(preview)[:240],
        "updatedAt": row.get("updated_at") or row.get("observed_at") or 0,
        "cwd": provider,
        "status": {"type": "idle"},
        "section": {"id": provider, "name": provider},
    }


def _input_text(params: Mapping[str, Any]) -> str:
    values: list[str] = []
    inputs = params.get("input")
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str):
                values.append(item["text"])
    return "\n".join(values).strip()


class Bridge:
    def __init__(self) -> None:
        self.remote_child: SelectedEnvironmentAppServer | None = None
        self.child: subprocess.Popen[str] | None = None
        relay = RemoteShellClient(actor_id=os.environ.get("COGNILODE_ACTOR_ID", "default"))
        try:
            selected = _environment_id(relay.current())
        except Exception:
            selected = None
        if selected:
            self.remote_child = SelectedEnvironmentAppServer(
                relay,
                executable=os.environ.get("CODEX_NATIVE_EXECUTABLE", "codex"),
                workdir=os.environ.get("CODEX_REMOTE_WORKDIR") or None,
                environment_id=selected,
            ).start()
        else:
            executable = os.environ.get("CODEX_NATIVE_EXECUTABLE", "codex")
            self.child = subprocess.Popen(
                [executable, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        self._write_lock = threading.Lock()
        self._pending: dict[str, tuple[Any, str]] = {}
        self._provider = None
        self.memory = AgentMemoryClient()

    @property
    def provider(self):
        if self._provider is None:
            self._provider = OpenAI().authenticate()
        return self._provider

    def emit(self, value: Mapping[str, Any]) -> None:
        with self._write_lock:
            sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def child_send(self, value: Mapping[str, Any]) -> None:
        if self.remote_child is not None:
            self.remote_child.send(value)
            return
        if self.child is None:
            raise RuntimeError("Codex App Server is unavailable.")
        if self.child.stdin is None:
            raise RuntimeError("Native Codex App Server stdin is unavailable.")
        self.child.stdin.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.child.stdin.flush()

    def _merge_thread_list(self, response: dict[str, Any]) -> dict[str, Any]:
        result = response.get("result")
        if not isinstance(result, dict):
            return response
        native = result.get("data") if isinstance(result.get("data"), list) else []
        external: list[dict[str, Any]] = []
        try:
            for row in _rows(self.memory.recent(limit=100)):
                summary = _memory_summary(row)
                if summary is not None:
                    external.append(summary)
        except Exception:
            pass
        result["data"] = [*external, *native]
        return response

    def _child_reader(self) -> None:
        if self.remote_child is not None:
            for message in self.remote_child.events():
                self._receive_child_message(message)
            return
        if self.child is None:
            return
        if self.child.stdout is None:
            return
        for line in self.child.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._receive_child_message(message)

    def _receive_child_message(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id in self._pending:
            original_id, operation = self._pending.pop(message_id)
            message["id"] = original_id
            if operation == "thread/list":
                message = self._merge_thread_list(message)
        self.emit(message)

    def _child_stderr(self) -> None:
        if self.child is None:
            return
        if self.child.stderr is None:
            return
        for line in self.child.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    def _forward_with_intercept(self, request: dict[str, Any], operation: str) -> None:
        internal_id = f"bridge:{uuid.uuid4()}"
        self._pending[internal_id] = (request.get("id"), operation)
        forwarded = dict(request)
        forwarded["id"] = internal_id
        self.child_send(forwarded)

    def _memory_read(self, conversation_id: str) -> dict[str, Any]:
        payload = self.memory.thread(conversation_id)
        if isinstance(payload, Mapping) and isinstance(payload.get("thread"), Mapping):
            return dict(payload["thread"])
        rows = _rows(payload)
        summary = _memory_summary(rows[0]) if rows else _memory_summary(
            payload if isinstance(payload, Mapping) else {"id": conversation_id}
        )
        return summary or {"id": f"memory:{conversation_id}", "turns": []}

    def _external_read(self, thread_id: str) -> dict[str, Any]:
        if thread_id.startswith("chatgpt:"):
            conversation_id = thread_id.split(":", 1)[1]
            conversation = self.provider.chatgpt.conversations.retrieve(conversation_id)
            try:
                self.memory.ingest_chatgpt_conversation(conversation)
            except Exception:
                pass
            return _provider_thread(conversation_id, conversation)
        if thread_id.startswith("memory:"):
            return self._memory_read(thread_id.split(":", 1)[1])
        raise ValueError("Not an external thread ID.")

    def _turn_worker(self, thread_id: str, turn_id: str, prompt: str) -> None:
        user_id = str(uuid.uuid4())
        user_item = {
            "id": user_id,
            "type": "userMessage",
            "content": [{"type": "text", "text": prompt}],
        }
        self.emit({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        self.emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "item": user_item}})
        try:
            if thread_id.startswith("chatgpt:"):
                conversation_id = thread_id.split(":", 1)[1]
                result = self.provider.chatgpt.operations.send(
                    prompt,
                    conversation_id=conversation_id,
                    user_message_id=user_id,
                )
            elif thread_id.startswith("memory:"):
                source_id = thread_id.split(":", 1)[1]
                markdown = self.memory.render_markdown(source_id)
                with tempfile.TemporaryDirectory(prefix="b4pt0r-conversation-") as directory:
                    source = os.path.join(directory, "conversation.md")
                    with open(source, "w", encoding="utf-8") as handle:
                        handle.write(markdown)
                    imported_prompt = "First, please read the attached conversation.md in full."
                    if prompt:
                        imported_prompt = f"{imported_prompt}\n\n{prompt}"
                    result = self.provider.chatgpt.operations.send(
                        imported_prompt,
                        attachment_paths=[source],
                        user_message_id=user_id,
                    )
            else:
                raise RuntimeError("Unsupported external conversation provider.")
            try:
                self.memory.ingest_chatgpt_turn(result)
            except Exception as error:
                self.emit(
                    {
                        "method": "cognilode/memoryIngestFailed",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "message": str(error),
                        },
                    }
                )
            turn = result.get("turn") if isinstance(result, Mapping) else None
            assistant = turn.get("assistant_message") if isinstance(turn, Mapping) else None
            assistant_id = str(assistant.get("id") if isinstance(assistant, Mapping) else uuid.uuid4())
            assistant_text = str(turn.get("assistant_text") if isinstance(turn, Mapping) else "")
            item = {"id": assistant_id, "type": "agentMessage", "text": assistant_text}
            self.emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "item": item}})
            self.emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "items": [user_item, item]}}})
        except Exception as error:
            self.emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "failed", "error": {"message": str(error)}, "items": [user_item]}}})

    def handle(self, request: dict[str, Any]) -> None:
        method = request.get("method")
        params = request.get("params") if isinstance(request.get("params"), Mapping) else {}
        thread_id = params.get("threadId")
        if method == "thread/list" and not params.get("cursor"):
            self._forward_with_intercept(request, "thread/list")
            return
        if isinstance(thread_id, str) and thread_id.startswith(("chatgpt:", "memory:")):
            request_id = request.get("id")
            if method in {"thread/read", "thread/resume"}:
                thread = self._external_read(thread_id)
                result: dict[str, Any] = {"thread": thread}
                if method == "thread/resume":
                    result.update({"cwd": thread.get("cwd", ""), "model": "gpt-5-6-thinking"})
                self.emit({"id": request_id, "result": result})
                return
            if method == "thread/turns/list":
                thread = self._external_read(thread_id)
                self.emit({"id": request_id, "result": {"data": list(reversed(thread.get("turns") or [])), "nextCursor": None}})
                return
            if method == "turn/start":
                prompt = _input_text(params)
                turn_id = str(uuid.uuid4())
                self.emit({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
                threading.Thread(
                    target=self._turn_worker,
                    args=(thread_id, turn_id, prompt),
                    daemon=True,
                ).start()
                return
        self.child_send(request)

    def run(self) -> int:
        threading.Thread(target=self._child_reader, daemon=True).start()
        if self.child is not None:
            threading.Thread(target=self._child_stderr, daemon=True).start()
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(request, dict):
                self.handle(request)
        if self.remote_child is not None:
            self.remote_child.close()
            return 0
        if self.child is None:
            return 1
        if self.child.stdin is not None:
            self.child.stdin.close()
        return self.child.wait()


def main(argv: Iterable[str] | None = None) -> int:
    del argv
    return Bridge().run()


if __name__ == "__main__":
    raise SystemExit(main())

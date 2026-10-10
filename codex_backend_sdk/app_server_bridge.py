"""Transparent Codex App Server multiplexer for native and normalized threads.

All unowned App Server methods and notifications pass through unchanged.  The
bridge owns only environment selection and normalized external conversation
projection.  Provider mutations go through the singular provider broker; this
process never reads provider credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import sys
import threading
from typing import Any, Iterable, Mapping, Sequence
import uuid

from .agent_memory import AgentMemoryClient
from .app_server_transport import (
    AppServerTransport,
    LocalAppServerTransport,
    RemoteAppServerTransport,
)
from .bridge_provider import ProviderCommandClient
from .remote_shell import RemoteShellClient


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("data", "items", "conversations", "results", "documents"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
    return []


def _cursor(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("nextCursor") or payload.get("next_cursor") or payload.get("cursor")
    return str(value) if value else None


def _input_text(params: Mapping[str, Any]) -> str:
    values: list[str] = []
    inputs = params.get("input")
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str):
                values.append(item["text"])
    if not values and isinstance(params.get("prompt"), str):
        values.append(str(params["prompt"]))
    return "\n".join(values).strip()


def _message_text(message: Mapping[str, Any]) -> str:
    for key in ("text", "markdown", "content"):
        value = message.get(key)
        if isinstance(value, str):
            return value
    content = message.get("content")
    if isinstance(content, Mapping):
        parts = content.get("parts")
        if isinstance(parts, list):
            return "\n".join(
                part if isinstance(part, str) else str(part.get("text") or "")
                for part in parts
                if isinstance(part, (str, Mapping))
            )
    return ""


def _event_turns(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    direct = payload.get("turns")
    if isinstance(direct, list):
        return [dict(turn) for turn in direct if isinstance(turn, Mapping)]
    messages = payload.get("messages") or payload.get("events") or payload.get("items") or []
    if not isinstance(messages, list):
        return []
    turns: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    active_id: str | None = None
    for value in messages:
        if not isinstance(value, Mapping):
            continue
        role = str(value.get("role") or value.get("author_role") or value.get("author") or "")
        event_id = str(value.get("event_id") or value.get("message_id") or value.get("id") or uuid.uuid4())
        text = _message_text(value)
        if role == "user":
            if active:
                turns.append({"id": active_id, "status": "completed", "items": active})
            active_id = str(value.get("turn_id") or f"memory-turn:{event_id}")
            active = [{"id": event_id, "type": "userMessage", "content": [{"type": "text", "text": text}]}]
        elif role == "assistant":
            if not active:
                active_id = str(value.get("turn_id") or f"memory-turn:{event_id}")
            active.append({"id": event_id, "type": "agentMessage", "text": text})
            if value.get("terminal") is True or value.get("end_turn") is True:
                turns.append({"id": active_id, "status": "completed", "items": active})
                active = []
                active_id = None
    if active:
        turns.append({"id": active_id, "status": "completed", "items": active})
    return turns


def _memory_summary(row: Mapping[str, Any]) -> dict[str, Any] | None:
    source = row.get("thread") if isinstance(row.get("thread"), Mapping) else row
    raw_id = source.get("conversation_id") or source.get("id") or source.get("thread_id")
    if not isinstance(raw_id, str) or not raw_id:
        return None
    if raw_id.startswith("memory:"):
        thread_id = raw_id
        conversation_id = raw_id.split(":", 1)[1]
    else:
        conversation_id = raw_id
        thread_id = f"memory:{raw_id}"
    provider = str(source.get("provider") or source.get("platform") or "memory")
    provider_conversation_id = source.get("provider_conversation_id")
    if not provider_conversation_id and provider.rstrip("/") in {"chatgpt", "chatgpt.com"}:
        provider_conversation_id = conversation_id
    name = source.get("title") or source.get("name") or source.get("subject") or raw_id
    preview = source.get("preview") or source.get("snippet") or source.get("text") or ""
    return {
        "id": thread_id,
        "name": str(name),
        "preview": str(preview)[:240],
        "updatedAt": source.get("updatedAt") or source.get("updated_at") or source.get("observed_at") or 0,
        "cwd": str(source.get("cwd") or provider),
        "status": source.get("status") if isinstance(source.get("status"), Mapping) else {"type": "idle"},
        "section": source.get("section") if isinstance(source.get("section"), Mapping) else {"id": provider, "name": provider},
        "source": {
            "kind": "agent_memory",
            "conversationId": conversation_id,
            "provider": provider,
            "providerConversationId": provider_conversation_id,
            "environmentId": source.get("environment_id"),
            "resumable": bool(source.get("resumable", True)),
        },
    }


def _memory_thread(conversation_id: str, payload: Any) -> dict[str, Any]:
    value: Mapping[str, Any]
    if isinstance(payload, Mapping) and isinstance(payload.get("thread"), Mapping):
        value = payload["thread"]
    elif isinstance(payload, Mapping):
        value = payload
    else:
        value = {"conversation_id": conversation_id}
    summary = _memory_summary({**dict(value), "conversation_id": conversation_id}) or {
        "id": f"memory:{conversation_id}", "turns": []
    }
    summary["turns"] = _event_turns(value)
    for key in ("provider", "provider_conversation_id", "parent_message_id", "environment_id", "model", "reasoning_effort"):
        if value.get(key) is not None:
            summary.setdefault("source", {})[key] = value[key]
    return summary


@dataclass
class Pending:
    original_id: Any
    operation: str
    request: dict[str, Any]


class Bridge:
    def __init__(self, native_args: Sequence[str]) -> None:
        self.native_args = tuple(native_args or ("app-server", "--stdio"))
        # Installed B4PT0R releases originally used CODEX_EXECUTABLE for the
        # bridge and CODEX_NATIVE_EXECUTABLE for the delegate.  New releases
        # select the bridge separately, but the delegate lookup must remain
        # backward compatible or an older desktop recursively launches itself.
        self.native_executable = (
            os.environ.get("CODEX_NATIVE_EXECUTABLE")
            or os.environ.get("CODEX_EXECUTABLE")
            or "codex"
        )
        self.actor_id = (
            os.environ.get("B4PT0R_ACTOR_ID")
            or os.environ.get("COGNILODE_ACTOR_ID")
            or "b4pt0r-desktop"
        )
        self.memory = AgentMemoryClient()
        self.provider = ProviderCommandClient()
        self.remote = RemoteShellClient(actor_id=self.actor_id)
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._pending: dict[str, Pending] = {}
        self._selected_environment = self._initial_environment()
        self._transport: AppServerTransport | None = None
        self._start_transport(self._selected_environment)

    def _initial_environment(self) -> str:
        configured = os.environ.get("COGNILODE_CODEX_ENVIRONMENT_ID")
        if configured:
            return configured
        try:
            current = self.remote.current()
        except Exception:
            return "local"
        if isinstance(current, Mapping):
            for key in ("environment_id", "environmentId", "selected_environment_id", "selectedEnvironmentId"):
                value = current.get(key)
                if value:
                    return str(value)
            selected = current.get("selected")
            if isinstance(selected, Mapping):
                value = selected.get("environment_id") or selected.get("id")
                if value:
                    return str(value)
        return str(current) if isinstance(current, str) and current else "local"

    def emit(self, value: Mapping[str, Any]) -> None:
        with self._write_lock:
            sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    @staticmethod
    def stderr(value: str) -> None:
        sys.stderr.write(value)
        sys.stderr.flush()

    def _start_transport(self, environment_id: str) -> None:
        # The outer B4PT0R bridge is the single unified conversation surface.
        # Disable the older Modified-Codex Python prompter inside the delegate;
        # otherwise two independent bridges compete and the inner one calls a
        # retired operator route before native Codex can answer.
        local_command = (
            "env",
            "CODEX_UNIFIED_PROMPTER=0",
            self.native_executable,
            *self.native_args,
        )
        remote_command = (
            "env",
            "CODEX_UNIFIED_PROMPTER=0",
            os.environ.get("B4PT0R_REMOTE_CODEX_EXECUTABLE", "codex"),
            *self.native_args,
        )
        with self._state_lock:
            prior = self._transport
            self._transport = None
            if prior is not None:
                prior.close()
            if environment_id == "local":
                self._transport = LocalAppServerTransport(
                    local_command, handler=self._native_message, stderr_handler=self.stderr
                )
            else:
                self._transport = RemoteAppServerTransport(
                    self.remote,
                    environment_id,
                    remote_command,
                    handler=self._native_message,
                    stderr_handler=self.stderr,
                )
            self._selected_environment = environment_id

    def native_send(self, value: Mapping[str, Any]) -> None:
        with self._state_lock:
            if self._transport is None:
                raise RuntimeError("native Codex App Server transport is unavailable")
            self._transport.send(value)

    def _native_message(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id in self._pending:
            pending = self._pending.pop(message_id)
            message["id"] = pending.original_id
            if pending.operation == "thread/list":
                message = self._merge_memory_list(message, pending.request)
            elif pending.operation == "thread/search":
                message = self._merge_memory_search(message, pending.request)
        self.emit(message)

    def _forward_intercept(self, request: dict[str, Any], operation: str) -> None:
        internal_id = f"bridge:{uuid.uuid4()}"
        self._pending[internal_id] = Pending(request.get("id"), operation, request)
        forwarded = dict(request)
        forwarded["id"] = internal_id
        self.native_send(forwarded)

    def _merge_memory_list(self, response: dict[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
        result = response.get("result")
        if not isinstance(result, dict):
            return response
        params = request.get("params") if isinstance(request.get("params"), Mapping) else {}
        # Native pagination remains authoritative. External summaries are added
        # only to the first page and have their own direct search surface.
        if params.get("cursor"):
            return response
        limit = max(1, min(int(params.get("limit") or 100), 500))
        filters = {
            key: params[key]
            for key in ("provider", "environment_id", "platform", "date_from", "date_to")
            if params.get(key) is not None
        }
        external: list[dict[str, Any]] = []
        try:
            payload = self.memory.list(limit=limit, **filters)
            for row in _rows(payload):
                summary = _memory_summary(row)
                if summary is not None:
                    external.append(summary)
        except Exception as exc:
            self.stderr(f"Agent Memory list unavailable: {type(exc).__name__}\n")
        native = result.get("data") if isinstance(result.get("data"), list) else []
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for row in [*external, *native]:
            if not isinstance(row, Mapping):
                continue
            identity = str(row.get("id") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            merged.append(dict(row))
        result["data"] = merged
        return response

    def _merge_memory_search(self, response: dict[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
        result = response.get("result")
        if not isinstance(result, dict):
            return response
        params = request.get("params") if isinstance(request.get("params"), Mapping) else {}
        query = str(params.get("query") or params.get("text") or "").strip()
        if not query:
            return response
        limit = max(1, min(int(params.get("limit") or 50), 500))
        external: list[dict[str, Any]] = []
        try:
            payload = self.memory.search(query, limit=limit)
            for row in _rows(payload):
                summary = _memory_summary(row)
                if summary is not None:
                    external.append(summary)
        except Exception as exc:
            self.stderr(f"Agent Memory search unavailable: {type(exc).__name__}\n")
        native = result.get("data") if isinstance(result.get("data"), list) else []
        result["data"] = [*external, *native]
        return response

    def _external_read(self, thread_id: str) -> dict[str, Any]:
        conversation_id = thread_id.split(":", 1)[1]
        payload = self.memory.project_thread(conversation_id)
        return _memory_thread(conversation_id, payload)

    def _turn_result_items(self, result: Mapping[str, Any]) -> tuple[dict[str, Any], list[Any]]:
        turn = result.get("turn") if isinstance(result.get("turn"), Mapping) else result
        assistant = turn.get("assistant_message") if isinstance(turn.get("assistant_message"), Mapping) else {}
        text = str(
            turn.get("assistant_text")
            or assistant.get("text")
            or result.get("text")
            or result.get("output")
            or ""
        )
        identity = str(assistant.get("id") or result.get("assistant_message_id") or uuid.uuid4())
        artifacts = turn.get("artifacts") or result.get("artifacts") or result.get("attachments") or []
        return {"id": identity, "type": "agentMessage", "text": text}, list(artifacts) if isinstance(artifacts, list) else []

    def _collected_turn(self, queued: Mapping[str, Any]) -> dict[str, Any]:
        job_id = str(queued.get("job_id") or "")
        if not job_id:
            raise RuntimeError("prompt queue omitted job identity")
        self.emit({"method": "cognilode/promptQueued", "params": {
            "jobId": job_id, "state": str(queued.get("state") or "queued")
        }})
        completed = self.provider.wait(job_id)
        if not completed.get("ok"):
            if completed.get("pending"):
                raise RuntimeError(f"provider operation remains queued: {job_id}")
            raise RuntimeError(f"provider operation ended in {completed.get('state')}: {job_id}")
        conversation_id = str(completed.get("conversation_id") or "")
        if not conversation_id:
            raise RuntimeError(f"completed provider operation omitted conversation identity: {job_id}")
        payload = self.memory.get(conversation_id)
        events = payload.get("events") if isinstance(payload, Mapping) else []
        assistant = {}
        if isinstance(events, list):
            for event in reversed(events):
                if isinstance(event, Mapping) and str(event.get("role") or "").lower() in {"assistant", "agent"}:
                    assistant = event
                    break
        text = str(assistant.get("content") or payload.get("response_excerpt") or "") if isinstance(payload, Mapping) else ""
        artifacts = payload.get("downloadable_files") if isinstance(payload, Mapping) else []
        return {
            "assistant_message_id": str(assistant.get("id") or assistant.get("provider_message_id") or uuid.uuid4()),
            "text": text,
            "artifacts": artifacts if isinstance(artifacts, list) else [],
            "conversation_id": conversation_id,
            "job_id": job_id,
        }

    def _turn_worker(self, thread: Mapping[str, Any], turn_id: str, prompt: str, params: Mapping[str, Any]) -> None:
        thread_id = str(thread["id"])
        user_id = str(uuid.uuid4())
        user_item = {"id": user_id, "type": "userMessage", "content": [{"type": "text", "text": prompt}]}
        self.emit({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        self.emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "item": user_item}})
        try:
            source = thread.get("source") if isinstance(thread.get("source"), Mapping) else {}
            provider = str(source.get("provider") or "memory").lower()
            conversation_id = str(source.get("conversationId") or thread_id.split(":", 1)[1])
            provider_conversation_id = source.get("provider_conversation_id") or source.get("providerConversationId")
            model = params.get("model") or source.get("model")
            effort = params.get("effort") or params.get("reasoningEffort") or source.get("reasoning_effort")
            requested_provider = str(params.get("provider") or provider)
            provider_kind = provider.rstrip("/").removesuffix(".com")
            requested_provider_kind = requested_provider.rstrip("/").removesuffix(".com")
            if provider_kind == "chatgpt" and provider_conversation_id and requested_provider_kind == "chatgpt":
                result = self.provider.continue_chatgpt(
                    conversation_id=str(provider_conversation_id),
                    prompt=prompt,
                    parent_message_id=source.get("parent_message_id"),
                    model=str(model) if model else None,
                    effort=str(effort) if effort else None,
                )
            else:
                result = self.provider.continue_from_memory(
                    source_conversation_id=conversation_id,
                    prompt=prompt,
                    destination_provider=requested_provider if requested_provider in {"chatgpt", "codex"} else "codex",
                    destination_conversation_id=str(provider_conversation_id) if provider_conversation_id else None,
                    environment_id=self._selected_environment,
                    model=str(model) if model else None,
                    effort=str(effort) if effort else None,
                )
            collected = self._collected_turn(result) if result.get("queued") else dict(result)
            assistant_item, artifacts = self._turn_result_items(collected)
            self.emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "item": assistant_item}})
            if artifacts:
                self.emit({"method": "cognilode/artifactsAvailable", "params": {"threadId": thread_id, "turnId": turn_id, "artifacts": artifacts}})
            self.emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "items": [user_item, assistant_item]}}})
        except Exception as exc:
            self.emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "failed", "error": {"message": str(exc)}, "items": [user_item]}}})

    def _environment_list(self, request_id: Any) -> None:
        value = self.remote.environments()
        nodes = value.get("nodes") if isinstance(value, Mapping) else value
        data = [
            {"environmentId": "local", "name": "Local", "selected": self._selected_environment == "local"},
            *[
                {**dict(node), "selected": str(node.get("environment_id") or node.get("id")) == self._selected_environment}
                for node in (nodes if isinstance(nodes, list) else [])
                if isinstance(node, Mapping)
            ],
        ]
        self.emit({"id": request_id, "result": {"data": data, "selectedEnvironmentId": self._selected_environment}})

    def _environment_select(self, request_id: Any, params: Mapping[str, Any]) -> None:
        environment_id = str(params.get("environmentId") or params.get("environment_id") or "").strip()
        if not environment_id:
            raise ValueError("environmentId is required")
        if environment_id != "local":
            self.remote.select(environment_id)
        self._start_transport(environment_id)
        self.emit({"id": request_id, "result": {"environmentId": environment_id, "selected": True}})

    def handle(self, request: dict[str, Any]) -> None:
        method = request.get("method")
        params = request.get("params") if isinstance(request.get("params"), Mapping) else {}
        thread_id = params.get("threadId") or params.get("thread_id")
        request_id = request.get("id")
        try:
            if method == "cognilode/environment/list":
                self._environment_list(request_id)
                return
            if method == "cognilode/environment/select":
                self._environment_select(request_id, params)
                return
            if method == "thread/list":
                self._forward_intercept(request, "thread/list")
                return
            if method == "thread/search":
                self._forward_intercept(request, "thread/search")
                return
            if isinstance(thread_id, str) and thread_id.startswith("memory:"):
                thread = self._external_read(thread_id)
                if method in {"thread/read", "thread/resume"}:
                    result: dict[str, Any] = {"thread": thread}
                    if method == "thread/resume":
                        result.update({"cwd": thread.get("cwd", ""), "model": thread.get("source", {}).get("model")})
                    self.emit({"id": request_id, "result": result})
                    return
                if method == "thread/turns/list":
                    turns = list(reversed(thread.get("turns") or []))
                    limit = max(1, min(int(params.get("limit") or len(turns) or 1), 500))
                    offset = int(params.get("cursor") or 0)
                    page = turns[offset : offset + limit]
                    next_cursor = str(offset + limit) if offset + limit < len(turns) else None
                    self.emit({"id": request_id, "result": {"data": page, "nextCursor": next_cursor}})
                    return
                if method == "turn/start":
                    prompt = _input_text(params)
                    if not prompt:
                        raise ValueError("turn input contains no text")
                    turn_id = str(uuid.uuid4())
                    self.emit({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
                    threading.Thread(target=self._turn_worker, args=(thread, turn_id, prompt, params), daemon=True).start()
                    return
            self.native_send(request)
        except Exception as exc:
            if request_id is not None:
                self.emit({"id": request_id, "error": {"code": -32000, "message": str(exc)}})
            else:
                self.stderr(f"bridge request {method}: {type(exc).__name__}\n")

    def run(self) -> int:
        try:
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(request, dict):
                    self.handle(request)
            return 0
        finally:
            with self._state_lock:
                if self._transport is not None:
                    self._transport.close()


def main(argv: Iterable[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    return Bridge(values or ["app-server", "--stdio"]).run()


if __name__ == "__main__":
    raise SystemExit(main())

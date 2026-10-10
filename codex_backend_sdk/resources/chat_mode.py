"""High-level ChatGPT Chat-mode turns composed from the SDK resources."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable, Mapping, TYPE_CHECKING
import uuid

if TYPE_CHECKING:
    from .._client import CodexClient


_EFFORT = {"medium": "standard", "high": "extended", "xhigh": "xhigh"}
_CONNECTOR_ID = re.compile(r"asdk_app_[A-Za-z0-9]{16,64}\Z")
_SANDBOX_PATH = re.compile(r"sandbox:(/(?:mnt/data|tmp)/[^\s)>\]]+)")


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content") if isinstance(message.get("content"), Mapping) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, Mapping):
            value = part.get("text") or part.get("content")
            if isinstance(value, str):
                texts.append(value)
    return "\n".join(value for value in texts if value)


def _connector_hints(connector_ids: Iterable[str]) -> list[str]:
    hints: list[str] = []
    for value in connector_ids:
        connector_id = value.strip()
        if not connector_id:
            continue
        if not _CONNECTOR_ID.fullmatch(connector_id):
            raise ValueError(f"Invalid ChatGPT connector ID: {connector_id}")
        hint = f"plugin:{connector_id}"
        if hint not in hints:
            hints.append(hint)
    return hints


def build_chat_mode_turn(
    prompt: str,
    *,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
    model: str = "gpt-5-6-thinking",
    effort: str = "high",
    connector_ids: Iterable[str] = (),
    attachments: Iterable[Mapping[str, Any]] = (),
    user_message_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the first-party ChatGPT conversation payload used by Desktop."""
    text = prompt.strip()
    if not text:
        raise ValueError("Chat-mode prompt must not be empty.")
    if effort not in _EFFORT:
        raise ValueError(f"Unsupported thinking effort: {effort}")
    message_id = str(uuid.UUID(user_message_id)) if user_message_id else str(uuid.uuid4())
    hints = _connector_hints(connector_ids)
    attachment_list = [dict(item) for item in attachments]
    metadata: dict[str, Any] = {}
    if hints:
        metadata["system_hints"] = hints
    if attachment_list:
        metadata["attachments"] = attachment_list

    payload: dict[str, Any] = {
        "action": "next",
        "messages": [
            {
                "id": message_id,
                "author": {"role": "user"},
                "create_time": time.time(),
                "content": {"content_type": "text", "parts": [text]},
                "metadata": metadata,
            }
        ],
        "parent_message_id": parent_message_id or "client-created-root",
        "model": model,
        "thinking_effort": _EFFORT[effort],
        "conversation_mode": {"kind": "primary_assistant"},
        "suggestions": [],
        "history_and_training_disabled": False,
        "force_paragen": False,
        "force_rate_limit": False,
        "websocket_request_id": str(uuid.uuid4()),
        "timezone_offset_min": 300,
        "timezone": "America/Chicago",
        "supported_encodings": ["v1"],
        "supports_buffering": True,
        "client_contextual_info": {"app_name": "chatgpt.com"},
        "force_parallel_switch": "auto",
        "client_prepare_state": "success",
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if hints:
        payload["system_hints"] = hints
    return message_id, payload


class _StreamSummary:
    def __init__(self, expected_user_message_id: str) -> None:
        self.expected_user_message_id = expected_user_message_id
        self.conversation_id: str | None = None
        self.accepted = False
        self.assistant_started = False
        self.assistant_end_turn = False
        self.assistant_status: str | None = None
        self.assistant_message_id: str | None = None
        self.assistant_text = ""
        self.errors: list[dict[str, Any]] = []

    def _absorb_message(self, message: Mapping[str, Any]) -> None:
        author = message.get("author") if isinstance(message.get("author"), Mapping) else {}
        if author.get("role") != "assistant":
            return
        self.assistant_started = True
        self.assistant_message_id = str(message.get("id") or self.assistant_message_id or "") or None
        text = _message_text(message)
        if text:
            self.assistant_text = text
        status = message.get("status")
        if isinstance(status, str):
            self.assistant_status = status
        metadata = message.get("metadata") if isinstance(message.get("metadata"), Mapping) else {}
        if message.get("end_turn") is True or metadata.get("finish_details") or metadata.get("is_complete"):
            self.assistant_end_turn = True

    def _patch(self, item: Mapping[str, Any]) -> None:
        path = item.get("p")
        value = item.get("v")
        if path == "/message/content/parts/0" and isinstance(value, str):
            self.assistant_started = True
            self.assistant_text += value
        elif path == "/message/end_turn" and value is True:
            self.assistant_end_turn = True
        elif path == "/message/status" and isinstance(value, str):
            self.assistant_status = value

    def absorb(self, event: Mapping[str, Any]) -> None:
        conversation_id = event.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            self.conversation_id = conversation_id
        nested = event.get("v")
        if isinstance(nested, Mapping):
            nested_conversation = nested.get("conversation_id")
            if isinstance(nested_conversation, str) and nested_conversation:
                self.conversation_id = nested_conversation
        if event.get("message_id") == self.expected_user_message_id or event.get("user_message_id") == self.expected_user_message_id:
            self.accepted = True
        message = event.get("message")
        if isinstance(message, Mapping):
            self._absorb_message(message)
        if isinstance(nested, Mapping) and isinstance(nested.get("message"), Mapping):
            self._absorb_message(nested["message"])
        if event.get("p") == "/message/content/parts/0" and isinstance(event.get("v"), str):
            self._patch(event)
        elif event.get("p") in ("", None) and isinstance(event.get("v"), str) and self.assistant_started:
            self.assistant_text += event["v"]
        if isinstance(event.get("v"), list):
            for item in event["v"]:
                if isinstance(item, Mapping):
                    self._patch(item)
        event_type = str(event.get("type") or "")
        if event_type.endswith((".failed", ".error")):
            self.errors.append({"type": event_type, "event": dict(event)})
        error = event.get("error")
        if isinstance(error, Mapping):
            self.errors.append(dict(error))
        elif isinstance(error, str):
            self.errors.append({"error": error})
        detail = event.get("detail")
        if isinstance(detail, str):
            self.errors.append({"detail": detail})

    def result(self) -> dict[str, Any]:
        return {
            "provider_acceptance_observed": self.accepted or bool(self.conversation_id),
            "assistant_end_turn": self.assistant_end_turn
            or self.assistant_status == "finished_successfully",
            "terminal_assistant_text": self.assistant_text,
            "terminal_assistant_message_id": self.assistant_message_id,
            "conversation_id": self.conversation_id,
            "provider_errors": self.errors,
        }


class ChatModeOperations:
    """Send and resolve ChatGPT Chat-mode work without a parallel HTTP stack."""

    def __init__(self, client: CodexClient) -> None:
        self._client = client

    def upload_attachments(self, paths: Iterable[str | Path]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for source in paths:
            path = Path(source).expanduser().resolve(strict=True)
            uploaded = self._client.chatgpt.files.upload(path)
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            uploaded["sha256"] = digest.hexdigest()
            attachments.append(uploaded)
        return attachments

    def _consume_stream(self, response: Any, user_message_id: str) -> dict[str, Any]:
        summary = _StreamSummary(user_message_id)
        try:
            for line in response.iter_lines(decode_unicode=True):
                if not isinstance(line, str) or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, Mapping):
                    summary.absorb(event)
        finally:
            response.close()
        return summary.result()

    def send(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        parent_message_id: str | None = None,
        model: str = "gpt-5-6-thinking",
        effort: str = "high",
        attachment_paths: Iterable[str | Path] = (),
        connector_ids: Iterable[str] = (),
        user_message_id: str | None = None,
        readback: bool = True,
        artifact_directory: str | Path | None = None,
    ) -> dict[str, Any]:
        if os.environ.get("B4PT0R_PROVIDER_CUSTODY_ACTIVE") != "1":
            raise RuntimeError(
                "direct ChatGPT mutation is disabled; use the queue-backed unified provider API"
            )
        parent = parent_message_id
        if conversation_id and not parent:
            raise ValueError(
                "Continuing a ChatGPT conversation requires its normalized parent_message_id; "
                "provider observability reads are disabled."
            )
        attachments = self.upload_attachments(attachment_paths)
        message_id, payload = build_chat_mode_turn(
            prompt,
            conversation_id=conversation_id,
            parent_message_id=parent,
            model=model,
            effort=effort,
            connector_ids=connector_ids,
            attachments=attachments,
            user_message_id=user_message_id,
        )
        response = self._client.chatgpt.conversations.create_stream(payload)
        streamed = self._consume_stream(response, message_id)
        resolved_conversation_id = str(streamed.get("conversation_id") or conversation_id or "")
        result: dict[str, Any] = {
            "user_message_id": message_id,
            "conversation_id": resolved_conversation_id or None,
            "attachments": attachments,
            "stream": streamed,
        }
        if readback:
            if not streamed.get("assistant_end_turn"):
                raise RuntimeError(
                    "ChatGPT stream ended without a terminal assistant turn; "
                    "the queue must retry the operation rather than issue observability reads."
                )
            assistant_text = str(streamed.get("terminal_assistant_text") or "")
            assistant_message_id = str(
                streamed.get("terminal_assistant_message_id") or ""
            )
            turn: dict[str, Any] = {
                "conversation_id": resolved_conversation_id or None,
                "user_message_id": message_id,
                "assistant_message_id": assistant_message_id or None,
                "assistant_text": assistant_text,
                "terminal": True,
            }
            if artifact_directory is not None and resolved_conversation_id and assistant_message_id:
                turn["artifacts"] = self.download_artifacts(
                    resolved_conversation_id,
                    assistant_message_id,
                    assistant_text,
                    artifact_directory,
                )
            result["turn"] = turn
        return result

    def collect(
        self,
        conversation_id: str,
        user_message_id: str,
        *,
        artifact_directory: str | Path | None = None,
    ) -> dict[str, Any]:
        turn = self._client.chatgpt.conversations.resolve_turn(
            conversation_id, user_message_id
        )
        assistant = turn.get("assistant_message")
        if not isinstance(assistant, Mapping):
            return turn
        turn["assistant_text"] = _message_text(assistant)
        metadata = assistant.get("metadata") if isinstance(assistant.get("metadata"), Mapping) else {}
        turn["model_receipt"] = {
            key: metadata[key]
            for key in (
                "model_slug",
                "resolved_model_slug",
                "default_model_slug",
                "thinking_effort",
            )
            if isinstance(metadata.get(key), str) and metadata[key]
        }
        if artifact_directory is not None:
            turn["artifacts"] = self.download_artifacts(
                conversation_id,
                str(assistant.get("id") or ""),
                turn["assistant_text"],
                artifact_directory,
            )
        return turn

    def download_artifacts(
        self,
        conversation_id: str,
        assistant_message_id: str,
        assistant_text: str,
        destination: str | Path,
    ) -> list[dict[str, Any]]:
        root = Path(destination).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        artifacts: list[dict[str, Any]] = []
        used: set[str] = set()
        for sandbox_path in dict.fromkeys(_SANDBOX_PATH.findall(assistant_text)):
            name = Path(sandbox_path).name or "artifact"
            candidate = name
            suffix = 2
            while candidate in used or (root / candidate).exists():
                candidate = f"{Path(name).stem}-{suffix}{Path(name).suffix}"
                suffix += 1
            used.add(candidate)
            output = root / candidate
            self._client.chatgpt.files.download_interpreter_artifact(
                conversation_id,
                assistant_message_id,
                sandbox_path,
                response_format="file",
                output_path=output,
            )
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            artifacts.append(
                {
                    "sandbox_path": sandbox_path,
                    "path": str(output),
                    "size": output.stat().st_size,
                    "sha256": digest,
                }
            )
        return artifacts


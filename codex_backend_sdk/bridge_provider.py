"""Credential-free provider-operation join for the App Server bridge."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Mapping, Sequence


class ProviderCommandClient:
    """Invoke the singular provider broker without reading provider secrets."""

    def __init__(self, command: Sequence[str] | None = None, *, timeout: int = 2100) -> None:
        if command is None:
            configured = os.environ.get("B4PT0R_PROVIDER_BROKER_COMMAND", "")
            if configured:
                value = json.loads(configured)
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError("B4PT0R_PROVIDER_BROKER_COMMAND must be a JSON argv array")
                command = value
        self.command = tuple(command or ())
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.command)

    def call(self, operation: str, request: Mapping[str, Any]) -> dict[str, Any]:
        if not self.command:
            raise RuntimeError("provider broker command is not configured")
        payload = {"operation": operation, **dict(request)}
        completed = subprocess.run(
            self.command,
            input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("provider broker returned no operation result")
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("provider broker returned malformed JSON") from exc
        if completed.returncode != 0 or result.get("ok") is False:
            raise RuntimeError(str(result.get("error") or result.get("message") or "provider operation failed"))
        return result

    def continue_chatgpt(
        self,
        *,
        conversation_id: str,
        prompt: str,
        parent_message_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "chatgpt_continue",
            {
                "conversation_id": conversation_id,
                "prompt": prompt,
                "parent_message_id": parent_message_id,
                "model": model,
                "effort": effort,
            },
        )

    def continue_from_memory(
        self,
        *,
        source_conversation_id: str,
        prompt: str,
        destination_provider: str,
        destination_conversation_id: str | None = None,
        environment_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "conversation_continue",
            {
                "source_conversation_id": source_conversation_id,
                "destination_provider": destination_provider,
                "destination_conversation_id": destination_conversation_id,
                "environment_id": environment_id,
                "prompt": prompt,
                "model": model,
                "effort": effort,
                "context_delivery": "rendered_conversation_attachment",
            },
        )


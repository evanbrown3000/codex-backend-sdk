"""Credential-free client for the singular custody-resident provider actuator."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Mapping, Sequence
import uuid


class ProviderActuationClient:
    def __init__(self, command: Sequence[str] | None = None, *, timeout: int = 2400) -> None:
        if command is None:
            raw = os.environ.get("B4PT0R_PROVIDER_BROKER_COMMAND", "")
            if raw:
                value = json.loads(raw)
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError("B4PT0R_PROVIDER_BROKER_COMMAND must be a JSON argv array")
                command = value
        self.command = tuple(command or ())
        self.timeout = timeout

    def _call(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not self.command:
            raise RuntimeError("credential custody broker command is not configured")
        completed = subprocess.run(
            self.command,
            input=json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")) + "\n",
            text=True, capture_output=True, timeout=self.timeout, check=False,
        )
        rows = [row for row in completed.stdout.splitlines() if row.strip()]
        if not rows:
            detail = completed.stderr.strip()[-2000:]
            raise RuntimeError("credential custody broker returned no result" +
                               ((": " + detail) if detail else ""))
        result = json.loads(rows[-1])
        if not isinstance(result, dict):
            raise RuntimeError("credential custody broker returned a non-object result")
        if completed.returncode and result.get("state") != "provider_error":
            raise RuntimeError(str(result.get("error") or result.get("message") or "provider actuation failed"))
        return result

    def capabilities(self) -> dict[str, Any]:
        return self._call({"operation": "provider_capabilities"})

    def prompt(
        self,
        provider: str,
        conversation: str | None,
        prompt: str,
        attachments: Sequence[str] = (),
        *,
        operation_id: str | None = None,
        account_id: str = "primary",
        parent_operation: Mapping[str, Any],
        mode: str | None = None,
        parent_message_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._call({
            "operation": "provider_prompt_with_lease",
            "operation_id": operation_id or str(uuid.uuid4()),
            "provider": provider,
            "account_id": account_id,
            "conversation_id": conversation,
            "parent_message_id": parent_message_id,
            "mode": mode or ("continue" if conversation else "create"),
            "prompt": prompt,
            "attachments": list(attachments),
            "model": model,
            "reasoning_effort": reasoning_effort,
            "parent_operation": dict(parent_operation),
            "metadata": dict(metadata or {}),
        })


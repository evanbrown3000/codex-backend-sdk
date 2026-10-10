"""Queue-backed provider operations for the unified App Server bridge.

The bridge owns no ChatGPT credential and performs no provider observation.
Interactive turns enter the central provider queue used by TaskFlow and
DecisionX. The biological-rhythm worker performs the actual B4PT0R operation;
this client observes only queue state and normalized Agent Memory.
"""

from __future__ import annotations

import os
import json
import subprocess
import time
from typing import Any, Mapping, Sequence
import uuid

import requests

from .operator_auth import operator_token


TERMINAL_STATES = {"completed", "failed", "cancelled", "held", "dead_letter"}


class ProviderCommandClient:
    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        timeout: int = 2400,
        poll_interval: float = 60.0,
    ) -> None:
        if command is None:
            configured = os.environ.get("B4PT0R_PROVIDER_BROKER_COMMAND", "")
            if configured:
                value = json.loads(configured)
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError("B4PT0R_PROVIDER_BROKER_COMMAND must be a JSON argv array")
                command = value
        self.command = tuple(command or ())
        self.endpoint = (
            endpoint
            or os.environ.get("COGNILODE_PROMPT_QUEUE_ENDPOINT")
            or "https://cognilode.com/api/operator/agent-memory"
        ).rstrip("/")
        self.token = operator_token(token)
        self.timeout = timeout
        self.poll_interval = max(15.0, poll_interval)
        self.session = requests.Session()

    def available(self) -> bool:
        return bool(self.command or self.token)

    def call(self, operation: str, request: Mapping[str, Any]) -> dict[str, Any]:
        if not self.command:
            raise RuntimeError("provider custody broker is not configured")
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
            raise RuntimeError("provider custody broker returned no operation result")
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("provider custody broker returned malformed JSON") from exc
        if completed.returncode != 0 or (isinstance(result, Mapping) and result.get("ok") is False):
            raise RuntimeError(str(result.get("error") or result.get("message") or "provider operation failed"))
        return result

    def _queue_call(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.token and self.command:
            value = self.call("agent_memory_request", {
                "method": "POST", "url": self.endpoint, "body": dict(payload), "params": {}
            })
            if isinstance(value, Mapping) and value.get("ok") is False:
                raise RuntimeError(str(value.get("error") or "prompt queue rejected operation"))
            return dict(value)
        headers = {"accept": "application/json", "content-type": "application/json"}
        if self.token:
            headers["authorization"] = "Bearer " + self.token
        response = self.session.post(
            self.endpoint, headers=headers, json=dict(payload), timeout=120
        )
        try:
            value = response.json()
        except ValueError as exc:
            raise RuntimeError(f"prompt queue returned HTTP {response.status_code} without JSON") from exc
        if response.status_code >= 400 or value.get("ok") is False:
            raise RuntimeError(str(value.get("error") or f"prompt queue HTTP {response.status_code}"))
        return value

    def _enqueue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = {
            "operation": "enqueue_job",
            "prompt_authority": "interactive_operator",
            "source": "b4pt0r-unified-app-server",
            "request_id": "interactive-" + uuid.uuid4().hex,
            **dict(payload),
        }
        value = self._queue_call(request)
        job = value.get("job") if isinstance(value.get("job"), Mapping) else {}
        job_id = str(value.get("id") or job.get("id") or request["request_id"])
        return {**value, "job_id": job_id, "queued": True}

    def wait(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        last: dict[str, Any] = {"job_id": job_id, "state": "queued"}
        while time.monotonic() < deadline:
            value = self._queue_call({"operation": "get_job", "job_id": job_id})
            job = value.get("job") if isinstance(value.get("job"), Mapping) else {}
            last = dict(job)
            state = str(job.get("state") or "").lower()
            conversation_id = self.conversation_id(job)
            # Older queue rows can retain effect_pending after the terminal
            # provider result was collected.  The completed timestamp plus a
            # provider-conversation effect is the durable completion receipt.
            completed = state == "completed" or bool(job.get("completed_at") and conversation_id)
            if completed or state in TERMINAL_STATES:
                return {
                    "ok": completed,
                    "job_id": job_id,
                    "state": "completed" if completed else state,
                    "conversation_id": conversation_id,
                    "job": last,
                }
            time.sleep(self.poll_interval)
        return {"ok": False, "job_id": job_id, "state": "queued", "job": last, "pending": True}

    @staticmethod
    def conversation_id(job: Mapping[str, Any]) -> str:
        for key in ("provider_conversation_id", "conversation_id"):
            value = str(job.get(key) or "").strip()
            if value:
                return value
        evidence = job.get("effect_evidence")
        if isinstance(evidence, list):
            for row in evidence:
                if isinstance(row, Mapping) and row.get("kind") in {
                    "provider_conversation", "central_conversation_readback"
                }:
                    value = str(row.get("ref") or "").strip()
                    if value:
                        return value
        return ""

    def continue_chatgpt(
        self,
        *,
        conversation_id: str,
        prompt: str,
        parent_message_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        return self._enqueue({
            "provider": "chatgpt.com",
            "conversation_id": conversation_id,
            "parent_message_id": parent_message_id,
            "prompt": prompt,
            "model": model,
            "reasoning_effort": effort,
        })

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
        kind = destination_provider.rstrip("/").removesuffix(".com")
        provider = "chatgpt.com" if kind == "chatgpt" else "codex.research"
        return self._enqueue({
            "provider": provider,
            "conversation_id": destination_conversation_id,
            "prompt": prompt,
            "source_conversation_id": source_conversation_id,
            "environment_id": environment_id,
            "model": model,
            "reasoning_effort": effort,
            "context_delivery": "central_normalized_conversation",
        })

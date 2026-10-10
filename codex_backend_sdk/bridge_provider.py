"""Queue-backed provider operations for the unified App Server bridge.

The bridge owns no ChatGPT credential and performs no provider observation.
Interactive turns enter the central provider queue used by TaskFlow and
DecisionX. The biological-rhythm worker performs the actual B4PT0R operation;
this client observes only queue state and normalized Agent Memory.
"""

from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Mapping, Sequence
import uuid

import requests

from .attachment_custody import commit_bytes, commit_path
from .operator_auth import operator_token
from .provider_scheduler_client import scheduler_available, scheduler_call


TERMINAL_STATES = {"complete", "completed", "provider_complete", "failed", "cancelled", "held", "dead_letter"}
PROVIDER_QUEUES = {
    "chatgpt": "chatgpt.com", "chatgpt.com": "chatgpt.com",
    "gemini": "gemini.com", "gemini.com": "gemini.com",
    "claude": "claude.com", "claude.com": "claude.com",
    "anthropic": "anthropic.com", "anthropic.com": "anthropic.com",
    "codex": "codex.research", "codex.research": "codex.research",
    "openai-codex": "codex.research", "codex.com": "codex.research",
}


def provider_queue(value: str) -> str:
    selected = PROVIDER_QUEUES.get(str(value).strip().casefold())
    if selected is None:
        raise ValueError("unsupported provider: " + str(value))
    return selected


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
        # The Python second-order scheduler is the exclusive mutation and
        # queue-state route.  Agent Memory remains the conversation authority.
        if scheduler_available():
            return scheduler_call(payload, timeout=min(self.timeout, 240))
        if str(payload.get("operation") or "") in {
            "enqueue_job", "begin_effect", "complete_job", "list_jobs",
            "rhythm_read", "rhythm_tick", "rhythm_device_heartbeat",
            "queue_ledger", "codex_admit", "codex_budget_update", "provider_cooldown",
        }:
            raise RuntimeError("central provider scheduler is unavailable")
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
        authority = str(payload.get("prompt_authority") or "interactive_operator")
        source = str(payload.get("source") or "b4pt0r-unified-app-server")
        request_id = str(payload.get("request_id") or "interactive-" + uuid.uuid4().hex)
        request = {
            "operation": "enqueue_job",
            **dict(payload),
            "prompt_authority": authority,
            "source": source,
            "request_id": request_id,
            "parent_operation": dict(payload.get("parent_operation") or {
                "operation_id": request_id,
                "automation_order": {
                    "taskflow_plan": 3, "rpe_run": 4, "decisionx": 5,
                }.get(authority, 2),
                "kind": authority if authority != "interactive_operator" else "human_interactive_queue_admission",
            }),
        }
        try:
            value = self._queue_call(request)
        except RuntimeError as exc:
            if str(exc) != "idempotency_conflict":
                raise
            # A durable batch can be resumed by another identical container
            # after its first admission response was lost.  Resolve only the
            # central queue identity (never provider history), and accept it
            # only when the immutable work identity still matches.
            existing_value = self._queue_call({"operation": "get_job", "job_id": request_id})
            existing = existing_value.get("job") if isinstance(existing_value.get("job"), Mapping) else {}
            prompt_hash = hashlib.sha256(str(request.get("prompt") or "").encode("utf-8")).hexdigest()
            expected_attachments = sorted(
                str(row.get("sha256") or "") for row in request.get("attachment_refs") or []
                if isinstance(row, Mapping)
            )
            actual_attachments = sorted(
                str(row.get("sha256") or "") for row in existing.get("attachment_refs") or []
                if isinstance(row, Mapping)
            )
            source_prompt_hash = str(
                existing.get("source_prompt_sha256")
                or (existing.get("interactive_operator_receipt") or {}).get("prompt_sha256")
                or existing.get("prompt_sha256")
                or ""
            )
            if (not existing
                    or source_prompt_hash != prompt_hash
                    or str(existing.get("provider") or "") != str(request.get("provider") or "")
                    or expected_attachments != actual_attachments):
                raise
            value = {**existing_value, "job": existing, "idempotent_existing": True}
        job = value.get("job") if isinstance(value.get("job"), Mapping) else {}
        job_id = str(value.get("id") or job.get("id") or request["request_id"])
        return {**value, "job_id": job_id, "queued": True}

    def stage_file(self, source: str | Path) -> dict[str, Any]:
        return commit_path(source)

    def stage_text(self, text: str, name: str = "conversation.md") -> dict[str, Any]:
        return commit_bytes(name, text.encode("utf-8"))

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
            completed = state in {"complete", "completed", "provider_complete"} or bool(job.get("completed_at") and conversation_id)
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
        attachments: Sequence[str] = (),
    ) -> dict[str, Any]:
        return self._enqueue({
            "provider": "chatgpt.com",
            "conversation_id": conversation_id,
            "parent_message_id": parent_message_id,
            "prompt": prompt,
            "model": model,
            "reasoning_effort": effort,
            "attachment_refs": [self.stage_file(path) for path in attachments],
        })

    def send_chatgpt(
        self,
        *,
        prompt: str,
        conversation_id: str | None = None,
        parent_message_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        attachments: Sequence[str] = (),
        request_id: str | None = None,
        project: str = "unified-b4pt0r",
        role: str = "interactive-operator",
        source: str = "b4pt0r-unified-cli",
        prompt_authority: str = "interactive_operator",
        priority: int | None = None,
        decisionx: Mapping[str, Any] | None = None,
        parent_operation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.send_provider(
            provider="chatgpt.com",
            prompt=prompt,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            model=model,
            effort=effort,
            attachments=attachments,
            request_id=request_id,
            project=project,
            role=role,
            source=source,
            prompt_authority=prompt_authority,
            priority=priority,
            decisionx=decisionx,
            parent_operation=parent_operation,
        )

    def send_provider(
        self,
        *,
        provider: str,
        prompt: str,
        conversation_id: str | None = None,
        parent_message_id: str | None = None,
        environment_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        attachments: Sequence[str] = (),
        request_id: str | None = None,
        project: str = "unified-b4pt0r",
        role: str = "interactive-operator",
        source: str = "b4pt0r-unified-cli",
        prompt_authority: str = "interactive_operator",
        priority: int | None = None,
        decisionx: Mapping[str, Any] | None = None,
        parent_operation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Admit any supported agent turn through one provider-neutral API.

        Each provider keeps its own queue clock and actuator.  This method owns
        neither provider credentials nor provider HTTP and therefore behaves
        identically from every environment.
        """
        selected = provider_queue(provider)
        return self._enqueue({
            "provider": selected,
            "conversation_id": conversation_id,
            "parent_message_id": parent_message_id,
            "environment_id": environment_id,
            "prompt": prompt,
            "model": model,
            "reasoning_effort": effort,
            "attachment_refs": [self.stage_file(path) for path in attachments],
            "request_id": request_id,
            "project": project,
            "role": role,
            "source": source,
            "prompt_authority": prompt_authority,
            "priority": priority,
            "decisionx": dict(decisionx) if decisionx is not None else None,
            "parent_operation": dict(parent_operation) if parent_operation is not None else None,
        })

    def queue_status(self, provider: str = "chatgpt.com") -> dict[str, Any]:
        return self._queue_call({"operation": "rhythm_read", "provider": provider})

    def queue_ledger(self, *, after_sequence: int = 0, limit: int = 200) -> dict[str, Any]:
        return self._queue_call({"operation": "queue_ledger", "after_sequence": after_sequence, "limit": limit})

    def codex_admit(self, *, estimated_cost: float, priority: int,
                    suitable_com_route: bool, parent_operation: Mapping[str, Any]) -> dict[str, Any]:
        return self._queue_call({"operation": "codex_admit", "estimated_cost": estimated_cost,
                                 "priority": priority, "suitable_com_route": suitable_com_route,
                                 "parent_operation": dict(parent_operation)})

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
        rendered_conversation: str | None = None,
        reduced_rollout: str | None = None,
        attachments: Sequence[str] = (),
    ) -> dict[str, Any]:
        provider = provider_queue(destination_provider)
        refs = [self.stage_file(path) for path in attachments]
        if reduced_rollout:
            refs.insert(0, self.stage_text(reduced_rollout, name="conversation.jsonl"))
            prompt = "First, please read the attached reduced Codex conversation rollout in full, then continue from that conversation.\n\n" + prompt
        elif rendered_conversation:
            refs.insert(0, self.stage_text(rendered_conversation))
            prompt = "First, please read the attached conversation.md in full, then continue from that conversation.\n\n" + prompt
        return self._enqueue({
            "provider": provider,
            "conversation_id": destination_conversation_id,
            "prompt": prompt,
            "source_conversation_id": source_conversation_id,
            "environment_id": environment_id,
            "model": model,
            "reasoning_effort": effort,
            "context_delivery": "central_normalized_conversation",
            "context_format": "reduced_codex_rollout" if reduced_rollout else "read_model_markdown",
            "attachment_refs": refs,
        })

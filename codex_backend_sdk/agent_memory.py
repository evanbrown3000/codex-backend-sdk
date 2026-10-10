"""Client for the centralized Agent Memory conversation surface."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import requests

from .bridge_provider import ProviderCommandClient
from .operator_auth import operator_token


class AgentMemoryClient:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        timeout: float = 120,
        use_broker: bool = True,
    ) -> None:
        selected_endpoint = (
            endpoint
            or os.environ.get("AGENT_MEMORY_ENDPOINT")
            or "https://cognilode.com/api/operator/conversations"
        ).rstrip("/")
        endpoint_path = urlsplit(selected_endpoint).path.rstrip("/")
        if not endpoint_path:
            selected_endpoint += "/api/operator/conversations"
            endpoint_path = "/api/operator/conversations"
        elif endpoint_path.endswith("/agent-memory"):
            selected_endpoint = selected_endpoint.rsplit("/agent-memory", 1)[0] + "/conversations"
            endpoint_path = urlsplit(selected_endpoint).path.rstrip("/")
        self.endpoint = selected_endpoint
        self.legacy_endpoint = (
            os.environ.get("AGENT_MEMORY_INGEST_ENDPOINT")
            or (
                self.endpoint.rsplit("/conversations", 1)[0] + "/agent-memory"
                if endpoint_path.endswith("/conversations")
                else self.endpoint.rstrip("/") + "/api/operator/agent-memory"
            )
        ).rstrip("/")
        self.foreground_endpoint = (
            self.endpoint.rsplit("/conversations", 1)[0] + "/foreground"
            if endpoint_path.endswith("/conversations")
            else self.endpoint.rstrip("/") + "/api/operator/foreground"
        )
        self.inventory_endpoint = os.environ.get("AGENT_MEMORY_INVENTORY_ENDPOINT") or (
            self.endpoint.rsplit("/v1/conversations", 1)[0] + "/v1/inventory/events"
            if "/v1/conversations" in self.endpoint
            else self.endpoint.rsplit("/api/operator/conversations", 1)[0] + "/api/operator/inventory/events"
        )
        self.token = operator_token(token)
        self.timeout = timeout
        self._session = requests.Session()
        self._broker = ProviderCommandClient(timeout=max(120, int(timeout))) if use_broker else None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        if self._broker is not None and self._broker.available() and not self.token:
            return self._broker.call(
                "agent_memory_request",
                {
                    "method": method,
                    "url": url,
                    "params": dict(params or {}),
                    "body": dict(body) if body is not None else None,
                },
            )
        response = self._session.request(
            method,
            url,
            headers=self._headers(),
            params={key: value for key, value in dict(params or {}).items() if value is not None},
            json=dict(body) if body is not None else None,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        projection: str = "app-server-summary",
        **filters: Any,
    ) -> Any:
        if self._broker is not None and self._broker.available() and not self.token:
            reply = self._broker.call("managed_control_request", {
                "action": "conversation_list", "arguments": {
                    "limit": limit, "cursor": cursor, "provider": filters.get("provider", "chatgpt.com")}})
            return reply["result"]
        params = {"limit": limit, "projection": projection, **filters}
        if cursor:
            params["offset"] = cursor
        return self._request("GET", self.endpoint, params=params)

    def recent(self, *, limit: int = 50, **filters: Any) -> Any:
        return self.list(limit=limit, **filters)

    def search(self, query: str, *, limit: int = 50, cursor: str | None = None, projection: str = "app-server-summary", **filters: Any) -> Any:
        if self._broker is not None and self._broker.available() and not self.token:
            reply = self._broker.call("managed_control_request", {
                "action": "conversation_search", "arguments": {
                    "query": query, "limit": limit, "provider": filters.get("provider", "chatgpt.com")}})
            return reply["result"]
        params = {"q": query, "limit": limit, "projection": projection, **filters}
        if cursor:
            params["offset"] = cursor
        return self._request("GET", self.endpoint, params=params)

    def get(self, conversation_id: str, **options: Any) -> Any:
        params = dict(options)
        return self._request("GET", f"{self.endpoint}/{quote(conversation_id, safe='')}", params=params)

    def thread(self, conversation_id: str, **options: Any) -> Any:
        return self.get(conversation_id, **options)

    def project_thread(self, conversation_id: str, *, projection: str = "app-server") -> Any:
        return self.get(conversation_id, projection=projection)

    def changes(self, conversation_id: str, *, after: str | None = None, **options: Any) -> Any:
        return self.get(conversation_id, cursor=after, **options)

    def read(
        self,
        conversation_id: str,
        *,
        reader_id: str,
        after: str | None = None,
        full: bool = False,
        peek: bool = False,
        projection: str = "read-model",
    ) -> Any:
        if self._broker is not None and self._broker.available() and not self.token:
            reply = self._broker.call("managed_control_request", {
                "action": "conversation_read", "arguments": {
                    "conversation_id": conversation_id, "reader_id": reader_id,
                    "after": after, "full": full, "peek": peek, "projection": projection}})
            return reply["result"]
        return self.get(
            conversation_id,
            cursor=None if full else after,
            reader_id=reader_id,
            peek=str(peek).lower(),
            projection=projection,
        )

    def render_markdown(self, conversation_id: str) -> str:
        result = self.get(conversation_id)
        if isinstance(result, dict):
            for key in ("rendered_markdown", "markdown", "content", "text"):
                if isinstance(result.get(key), str):
                    return result[key]
        raise RuntimeError("Agent Memory response omitted rendered conversation Markdown.")

    def prompt_foreground(
        self,
        *,
        persona: str = "company",
        max_tokens: int = 40_000,
        task: str = "",
    ) -> dict[str, Any]:
        value = self._request(
            "POST",
            self.foreground_endpoint,
            body={"persona": persona, "max_tokens": max_tokens, "task": task},
        )
        if isinstance(value, Mapping) and isinstance(value.get("result"), Mapping):
            value = value["result"]
        if not isinstance(value, Mapping) or not str(value.get("context") or "").strip():
            raise RuntimeError("Agent Memory response omitted selected prompt foreground")
        return dict(value)

    def reduced_rollout(self, conversation_id: str) -> dict[str, Any]:
        value = self._request(
            "GET",
            f"{self.endpoint}/{quote(conversation_id, safe='')}/reduced-rollout",
        )
        if isinstance(value, Mapping) and isinstance(value.get("result"), Mapping):
            value = value["result"]
        if not isinstance(value, Mapping) or not str(value.get("content") or "").strip():
            raise RuntimeError("Agent Memory response omitted reduced Codex rollout")
        return dict(value)

    def ingest(self, observation: Mapping[str, Any]) -> Any:
        return self._request("POST", self.legacy_endpoint, body={"operation": "record_conversation", **dict(observation)})

    def admit_inventory(self, event: Mapping[str, Any]) -> Any:
        return self._request("POST", self.inventory_endpoint, body={"events": [dict(event)]})

    def admit_summary_result(
        self,
        *,
        job_id: str,
        summary: str,
        worker_conversation_url: str,
        source_ref: str = "b4pt0r-provider-terminal",
    ) -> Any:
        """Bind one terminal provider report to its exact queued Memory job."""
        return self._request("POST", self.legacy_endpoint, body={
            "operation": "memory_stock.chatgpt_summary_admit",
            "job_id": job_id,
            "summary": summary,
            "worker_conversation_url": worker_conversation_url,
            "manual_read_attestation": "read_every_source_fragment_in_full_without_skipping",
            "worker_persona_id": "memory_stock",
            "source_ref": source_ref,
        })

    def expand(self, request: Mapping[str, Any]) -> Any:
        return self._request("POST", self.legacy_endpoint, body={"operation": "conversation_expand", **dict(request)})

    def ingest_chatgpt_turn(
        self,
        result: Mapping[str, Any],
        *,
        environment_id: str | None = None,
        account_id: str | None = None,
    ) -> Any:
        turn = result.get("turn") if isinstance(result.get("turn"), Mapping) else {}
        messages = [
            value
            for value in (turn.get("user_message"), turn.get("assistant_message"))
            if isinstance(value, Mapping)
        ]
        observation = {
            "schema": "memory_stock.conversation_observation.v1",
            "provider": "chatgpt.com",
            "provider_conversation_id": result.get("conversation_id"),
            "conversation_id": result.get("conversation_id"),
            "provider_user_message_id": result.get("user_message_id"),
            "environment_id": environment_id or os.environ.get("COGNILODE_REMOTE_ENVIRONMENT_ID"),
            "provider_account_id": account_id or os.environ.get("B4PT0R_CHATGPT_ACCOUNT_ID"),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "type": "b4pt0r_sdk_chat_mode",
                "provenance": "provider_stream_and_exact_branch_readback",
            },
            "messages": messages,
            "attachments": result.get("attachments") or [],
            "artifacts": turn.get("artifacts") or [],
            "terminal": turn.get("terminal") is True,
        }
        return self.ingest(observation)

    def record_chatgpt_turn(
        self,
        *,
        conversation_id: str,
        user_message_id: str,
        assistant_message_id: str,
        prompt: str,
        response: str,
        attachments: list[Mapping[str, Any]],
        artifacts: list[Mapping[str, Any]],
        provider_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Admit a streamed turn and verify it through central memory only."""
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        response_hash = hashlib.sha256(response.encode("utf-8")).hexdigest()
        recorded = self.ingest({
            "provider": "chatgpt.com",
            "provider_conversation_id": conversation_id,
            "conversation_id": conversation_id,
            "title": "B4PT0R Chat-mode Prompt Dispatch",
            "conversation_url": f"https://chatgpt.com/c/{conversation_id}",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "type": "b4pt0r_sdk_chat_mode",
                "provenance": "provider_stream_terminal_without_provider_readback",
            },
            "messages": [
                {
                    "id": user_message_id,
                    "role": "user",
                    "text": prompt,
                    "metadata": {"attachments": [dict(row) for row in attachments]},
                },
                {
                    "id": assistant_message_id,
                    "parent": user_message_id,
                    "role": "assistant",
                    "text": response,
                    "metadata": {"provider_receipt": dict(provider_receipt)},
                },
            ],
            "attachments": [dict(row) for row in attachments],
            "artifacts": [dict(row) for row in artifacts],
            "terminal": True,
        })
        result = recorded.get("result") if isinstance(recorded, Mapping) else None
        result = result if isinstance(result, Mapping) else recorded
        changed = result.get("conversations") if isinstance(result, Mapping) else []
        stored_id = str(changed[0].get("conversation_id") or "") if changed else ""
        if not stored_id:
            raise RuntimeError("central memory ingestion omitted conversation identity")
        readback = self.get(stored_id)
        readback = readback.get("result") if isinstance(readback, Mapping) and isinstance(readback.get("result"), Mapping) else readback
        events = list(readback.get("events") or []) if isinstance(readback, Mapping) else []
        prompt_event = next((
            row for row in events
            if isinstance(row, Mapping)
            and user_message_id in tuple(row.get("provider_message_ids") or ())
        ), {})
        response_event = next((
            row for row in events
            if isinstance(row, Mapping)
            and assistant_message_id in tuple(row.get("provider_message_ids") or ())
        ), {})
        actual_prompt = hashlib.sha256(str(prompt_event.get("text") or "").encode("utf-8")).hexdigest()
        actual_response = hashlib.sha256(str(response_event.get("text") or "").encode("utf-8")).hexdigest()
        if actual_prompt != prompt_hash or actual_response != response_hash:
            raise RuntimeError("central memory turn readback mismatch")
        return {
            "ok": True,
            "stored": True,
            "conversation_id": stored_id,
            "central_readback_verified": True,
            "central_prompt_sha256": actual_prompt,
            "central_response_sha256": actual_response,
            "provider_requests_created_by_readback": 0,
        }

    def ingest_chatgpt_conversation(
        self,
        conversation: Mapping[str, Any],
        *,
        environment_id: str | None = None,
        account_id: str | None = None,
    ) -> Any:
        provider_id = str(conversation.get("conversation_id") or conversation.get("id") or "")
        mapping = conversation.get("mapping") if isinstance(conversation.get("mapping"), Mapping) else {}
        messages: list[Mapping[str, Any]] = []
        current = conversation.get("current_node")
        visited: set[str] = set()
        while isinstance(current, str) and current and current not in visited:
            visited.add(current)
            node = mapping.get(current)
            if not isinstance(node, Mapping):
                break
            message = node.get("message")
            if isinstance(message, Mapping):
                messages.append(message)
            current = node.get("parent")
        messages.reverse()
        return self.ingest(
            {
                "schema": "memory_stock.conversation_observation.v1",
                "provider": "chatgpt.com",
                "provider_conversation_id": provider_id,
                "conversation_id": provider_id,
                "environment_id": environment_id or os.environ.get("COGNILODE_REMOTE_ENVIRONMENT_ID"),
                "provider_account_id": account_id or os.environ.get("B4PT0R_CHATGPT_ACCOUNT_ID"),
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": {
                    "type": "b4pt0r_sdk_provider_read",
                    "provenance": "provider_native_conversation_graph",
                },
                "messages": messages,
            }
        )

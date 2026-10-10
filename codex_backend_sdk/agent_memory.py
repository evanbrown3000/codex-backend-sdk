"""Client for the centralized Agent Memory conversation surface."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Mapping
from urllib.parse import quote

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
        self.endpoint = (
            endpoint
            or os.environ.get("AGENT_MEMORY_ENDPOINT")
            or "https://cognilode.com/api/operator/conversations"
        ).rstrip("/")
        self.legacy_endpoint = (
            os.environ.get("AGENT_MEMORY_INGEST_ENDPOINT")
            or self.endpoint.rsplit("/conversations", 1)[0] + "/agent-memory"
        ).rstrip("/")
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
        params = {"limit": limit, **filters}
        if cursor:
            params["offset"] = cursor
        return self._request("GET", self.endpoint, params=params)

    def recent(self, *, limit: int = 50, **filters: Any) -> Any:
        return self.list(limit=limit, **filters)

    def search(self, query: str, *, limit: int = 50, cursor: str | None = None, projection: str = "app-server-summary", **filters: Any) -> Any:
        params = {"q": query, "limit": limit, **filters}
        if cursor:
            params["offset"] = cursor
        return self._request("GET", self.endpoint, params=params)

    def get(self, conversation_id: str, **options: Any) -> Any:
        params = dict(options)
        params.pop("projection", None)
        return self._request("GET", f"{self.endpoint}/{quote(conversation_id, safe='')}", params=params)

    def thread(self, conversation_id: str, **options: Any) -> Any:
        return self.get(conversation_id, **options)

    def project_thread(self, conversation_id: str, *, projection: str = "app-server") -> Any:
        return self.get(conversation_id)

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
        return self.get(
            conversation_id,
            cursor=None if full else after,
            reader_id=reader_id,
            peek=str(peek).lower(),
        )

    def render_markdown(self, conversation_id: str) -> str:
        result = self.get(conversation_id)
        if isinstance(result, dict):
            for key in ("rendered_markdown", "markdown", "content", "text"):
                if isinstance(result.get(key), str):
                    return result[key]
        raise RuntimeError("Agent Memory response omitted rendered conversation Markdown.")

    def ingest(self, observation: Mapping[str, Any]) -> Any:
        return self._request("POST", self.legacy_endpoint, body={"operation": "ingest_conversation", **dict(observation)})

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

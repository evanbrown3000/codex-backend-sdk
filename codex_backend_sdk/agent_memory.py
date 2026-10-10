"""Focused client for the central Agent Memory conversation facade."""

from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Mapping

import requests

from .bridge_provider import ProviderCommandClient


class AgentMemoryClient:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        token_file: str | Path | None = None,
        timeout: float = 120,
    ) -> None:
        self.endpoint = (
            endpoint
            or os.environ.get("AGENT_MEMORY_ENDPOINT")
            or "https://cognilode.com/api/operator/agent-memory"
        ).rstrip("/")
        configured_file = token_file or os.environ.get("COGNILODE_OPERATOR_TOKEN_FILE")
        file_token = ""
        if configured_file:
            try:
                file_token = Path(configured_file).expanduser().read_text(encoding="utf-8").strip()
            except OSError:
                file_token = ""
        self.token = (token or os.environ.get("COGNILODE_OPERATOR_TOKEN") or file_token).removeprefix("Bearer ").strip()
        self.timeout = timeout
        self._session = requests.Session()
        self._broker = ProviderCommandClient(timeout=max(120, int(timeout)))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        if self._broker.available():
            return self._broker.call(
                "agent_memory_request",
                {
                    "method": method,
                    "path": path,
                    "params": dict(params or {}),
                    "body": dict(body) if body is not None else None,
                },
            )
        response = self._session.request(
            method,
            f"{self.endpoint}/{path.lstrip('/')}",
            headers=self._headers(),
            params=dict(params or {}),
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
        params = {"limit": limit, "projection": projection, **filters}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "v1/conversations", params=params)

    def recent(self, *, limit: int = 50, **filters: Any) -> Any:
        return self.list(limit=limit, **filters)

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
        projection: str = "app-server-summary",
        **filters: Any,
    ) -> Any:
        body = {"query": query, "limit": limit, "projection": projection, **filters}
        if cursor:
            body["cursor"] = cursor
        return self._request(
            "POST", "v1/conversations/search", body=body
        )

    def get(self, conversation_id: str, **options: Any) -> Any:
        return self._request("GET", f"v1/conversations/{conversation_id}", params=options)

    def thread(self, conversation_id: str, **options: Any) -> Any:
        return self.get(conversation_id, **options)

    def project_thread(self, conversation_id: str, *, projection: str = "app-server") -> Any:
        return self.get(conversation_id, projection=projection)

    def changes(self, conversation_id: str, *, after: str | None = None, **options: Any) -> Any:
        params = dict(options)
        if after is not None:
            params["after"] = after
        return self._request(
            "GET", f"v1/conversations/{conversation_id}/changes", params=params
        )

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
        if full:
            return self.get(
                conversation_id,
                reader_id=reader_id,
                peek=str(peek).lower(),
                projection=projection,
            )
        return self.changes(
            conversation_id,
            after=after,
            reader_id=reader_id,
            peek=str(peek).lower(),
            projection=projection,
        )

    def render_markdown(self, conversation_id: str) -> str:
        result = self._request(
            "GET",
            f"v1/conversations/{conversation_id}/render",
            params={"format": "markdown"},
        )
        if isinstance(result, dict):
            for key in ("markdown", "content", "text"):
                if isinstance(result.get(key), str):
                    return result[key]
        if isinstance(result, str):
            return result
        raise RuntimeError("Agent Memory render response omitted Markdown content.")

    def ingest(self, observation: Mapping[str, Any]) -> Any:
        return self._request("POST", "v1/conversations/ingest", body=observation)

    def expand(self, request: Mapping[str, Any]) -> Any:
        return self._request("POST", "v1/conversations/expand", body=request)

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
            "provider": "chatgpt",
            "provider_conversation_id": result.get("conversation_id"),
            "conversation_id": f"chatgpt:{result.get('conversation_id')}",
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
                "provider": "chatgpt",
                "provider_conversation_id": provider_id,
                "conversation_id": f"chatgpt:{provider_id}",
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

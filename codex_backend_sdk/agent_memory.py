"""Focused client for the central Agent Memory conversation facade."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

import requests


class AgentMemoryClient:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        timeout: float = 120,
    ) -> None:
        self.endpoint = (
            endpoint
            or os.environ.get("AGENT_MEMORY_ENDPOINT")
            or "https://cognilode.com/api/operator/agent-memory"
        ).rstrip("/")
        self.token = token or os.environ.get("COGNILODE_OPERATOR_TOKEN")
        self.timeout = timeout
        self._session = requests.Session()

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

    def list(self, *, limit: int = 50, **filters: Any) -> Any:
        return self._request("GET", "v1/conversations", params={"limit": limit, **filters})

    def recent(self, *, limit: int = 50, **filters: Any) -> Any:
        return self.list(limit=limit, **filters)

    def search(self, query: str, *, limit: int = 20, **filters: Any) -> Any:
        return self._request(
            "POST", "v1/conversations/search", body={"query": query, "limit": limit, **filters}
        )

    def get(self, conversation_id: str, **options: Any) -> Any:
        return self._request("GET", f"v1/conversations/{conversation_id}", params=options)

    def thread(self, conversation_id: str, **options: Any) -> Any:
        return self.get(conversation_id, **options)

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

"""Focused client for the central Agent Memory conversation facade."""

from __future__ import annotations

import os
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

    def recent(self, *, limit: int = 50, **filters: Any) -> Any:
        return self._request("GET", "v1/kb/recent", params={"limit": limit, **filters})

    def search(self, query: str, *, limit: int = 20, **filters: Any) -> Any:
        return self._request(
            "POST", "v1/kb/search", body={"query": query, "limit": limit, **filters}
        )

    def get(self, conversation_id: str, **options: Any) -> Any:
        return self._request(
            "GET", "v1/kb/get", params={"conversation_id": conversation_id, **options}
        )

    def thread(self, conversation_id: str, **options: Any) -> Any:
        return self._request(
            "GET", "v1/kb/thread", params={"conversation_id": conversation_id, **options}
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
        return self._request(
            "POST",
            "v1/conversations/read",
            body={
                "conversation_id": conversation_id,
                "reader_id": reader_id,
                "mode": "full" if full else "adaptive",
                "peek": peek,
                "projection": projection,
            },
        )

    def render_markdown(self, conversation_id: str) -> str:
        result = self._request(
            "GET",
            "v1/conversations/render",
            params={"conversation_id": conversation_id, "format": "markdown"},
        )
        if isinstance(result, dict):
            for key in ("markdown", "content", "text"):
                if isinstance(result.get(key), str):
                    return result[key]
        if isinstance(result, str):
            return result
        raise RuntimeError("Agent Memory render response omitted Markdown content.")


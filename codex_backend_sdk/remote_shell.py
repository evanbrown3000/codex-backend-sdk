"""Client for the existing Cognilode remote-shell MCP relay."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping
import uuid

import requests

from .bridge_provider import ProviderCommandClient
from .operator_auth import operator_token


class RemoteShellClient:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        actor_id: str = "default",
        timeout: float = 120,
    ) -> None:
        self.endpoint = (
            endpoint
            or os.environ.get("COGNILODE_REMOTE_SHELL_ENDPOINT")
            or "https://cognilode.com/api/operator/remote-shell/mcp"
        )
        self.token = operator_token(token)
        self.actor_id = actor_id
        self.timeout = timeout
        self._session = requests.Session()
        self._selected_environment: str | None = None
        self._broker = ProviderCommandClient(timeout=max(120, int(timeout)))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        if self._broker.available() and not self.token:
            return self._broker.call(
                "remote_shell_request",
                {"name": name, "arguments": dict(arguments or {}), "actor_id": self.actor_id},
            )
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments or {})},
        }
        response = self._session.post(
            self.endpoint,
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict) and body.get("error"):
            error = body["error"]
            raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            return result
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        blocks = result.get("content")
        if isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], dict):
            text = blocks[0].get("text")
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return result

    def environments(self) -> Any:
        return self.call("node_list")

    def select(self, environment_id: str) -> Any:
        result = self.call(
            "environment_select",
            {"actor_id": self.actor_id, "environment_id": environment_id},
        )
        self._selected_environment = environment_id
        return result

    def current(self) -> Any:
        try:
            result = self.call("environment_current", {"actor_id": self.actor_id})
            if isinstance(result, Mapping):
                selected = result.get("environment_id") or result.get("selected_environment_id")
                if isinstance(selected, str) and selected:
                    self._selected_environment = selected
            return result
        except RuntimeError:
            return {
                "actor_id": self.actor_id,
                "environment_id": self._selected_environment,
                "source": "bridge_process_cache",
            }

    def execute(
        self,
        command: str,
        *,
        workdir: str | None = None,
        environment_id: str | None = None,
        yield_time_ms: int = 9000,
        max_output_tokens: int | None = None,
    ) -> Any:
        arguments: dict[str, Any] = {
            "cmd": command,
            "yield_time_ms": yield_time_ms,
            "actor_id": self.actor_id,
        }
        if workdir:
            arguments["workdir"] = workdir
        if environment_id:
            arguments["environment_id"] = environment_id
        if max_output_tokens is not None:
            arguments["max_output_tokens"] = max_output_tokens
        return self.call("exec_command", arguments)

    def write(
        self,
        process_id: str | int,
        *,
        chars: str = "",
        environment_id: str | None = None,
        yield_time_ms: int = 9000,
        max_output_tokens: int | None = None,
    ) -> Any:
        arguments: dict[str, Any] = {
            "process_id": process_id,
            "chars": chars,
            "yield_time_ms": yield_time_ms,
            "actor_id": self.actor_id,
        }
        if environment_id:
            arguments["environment_id"] = environment_id
        if max_output_tokens is not None:
            arguments["max_output_tokens"] = max_output_tokens
        return self.call("write_stdin", arguments)

    def terminate(
        self, process_id: str | int, *, environment_id: str | None = None
    ) -> Any:
        arguments: dict[str, Any] = {"process_id": process_id, "actor_id": self.actor_id}
        if environment_id:
            arguments["environment_id"] = environment_id
        try:
            return self.call("terminate", arguments)
        except RuntimeError:
            return self.write(
                process_id,
                chars="\x03",
                environment_id=environment_id,
                yield_time_ms=1000,
            )

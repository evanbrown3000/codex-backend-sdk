"""Codex App Server over the persistently selected remote environment.

The existing Cognilode remote-shell relay owns routing and process lifecycle.
This module only converts its incremental process stream into the JSONL App
Server protocol already consumed by the B4PT0R bridge.
"""

from __future__ import annotations

import json
from queue import Queue
import shlex
import threading
from typing import Any, Iterator, Mapping, Sequence

from .remote_shell import RemoteShellClient


class RemoteAppServerError(RuntimeError):
    pass


class SelectedEnvironmentAppServer:
    """A running Codex App Server in the caller's selected environment."""

    def __init__(
        self,
        relay: RemoteShellClient,
        *,
        executable: str = "codex",
        arguments: Sequence[str] = ("app-server", "--stdio"),
        workdir: str | None = None,
        environment_id: str | None = None,
        poll_ms: int = 9000,
    ) -> None:
        self.relay = relay
        self.executable = executable
        self.arguments = tuple(arguments)
        self.workdir = workdir
        self.environment_id = environment_id
        self.poll_ms = poll_ms
        self.process_id: str | None = None
        self._events: Queue[dict[str, Any] | BaseException | None] = Queue()
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._buffer = ""
        self._prior_output = ""
        self._closed = threading.Event()

    def start(self) -> "SelectedEnvironmentAppServer":
        if self.process_id is not None:
            return self
        environment = self.environment_id or _environment_id(self.relay.current())
        if not environment:
            raise RemoteAppServerError("no remote environment is selected")
        self.environment_id = environment
        command = "exec " + " ".join(
            shlex.quote(value) for value in (self.executable, *self.arguments)
        )
        result = self.relay.execute(
            command,
            workdir=self.workdir,
            environment_id=environment,
            yield_time_ms=250,
        )
        self.process_id = _process_id(result)
        if not self.process_id:
            raise RemoteAppServerError("remote shell did not return a process identifier")
        self._consume(result)
        self._reader = threading.Thread(target=self._read_forever, daemon=True)
        self._reader.start()
        return self

    def send(self, message: Mapping[str, Any]) -> None:
        if self.process_id is None or self._closed.is_set():
            raise RemoteAppServerError("remote App Server is not running")
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            result = self.relay.write(
                self.process_id,
                chars=line,
                environment_id=self.environment_id,
                yield_time_ms=250,
            )
            self._consume(result)

    def events(self) -> Iterator[dict[str, Any]]:
        self.start()
        while True:
            value = self._events.get()
            if value is None:
                return
            if isinstance(value, BaseException):
                raise RemoteAppServerError(str(value)) from value
            yield value

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self.process_id is not None:
            try:
                self.relay.write(
                    self.process_id,
                    chars="\x04",
                    environment_id=self.environment_id,
                    yield_time_ms=250,
                )
            except Exception:
                pass
        self._events.put(None)

    def _read_forever(self) -> None:
        try:
            while not self._closed.is_set() and self.process_id is not None:
                with self._write_lock:
                    result = self.relay.write(
                        self.process_id,
                        environment_id=self.environment_id,
                        yield_time_ms=self.poll_ms,
                    )
                    self._consume(result)
                if _terminal(result):
                    self._flush_tail()
                    self._events.put(None)
                    self._closed.set()
                    return
        except BaseException as exc:
            self._events.put(exc)
            self._closed.set()

    def _consume(self, result: Any) -> None:
        output = _output(result)
        if not output:
            return
        # Gateways may return either an incremental chunk or cumulative output.
        if self._prior_output and output.startswith(self._prior_output):
            chunk = output[len(self._prior_output):]
            self._prior_output = output
        else:
            chunk = output
            self._prior_output = output
        self._buffer += chunk
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # Codex App Server stdout is JSONL. Non-JSON process output is
                # retained until the next chunk in case the gateway split it.
                self._buffer = line + "\n" + self._buffer
                return
            if isinstance(value, dict):
                self._events.put(value)

    def _flush_tail(self) -> None:
        tail = self._buffer.strip()
        self._buffer = ""
        if not tail:
            return
        try:
            value = json.loads(tail)
        except json.JSONDecodeError as exc:
            self._events.put(RemoteAppServerError("remote App Server ended with malformed JSONL"))
            return
        if isinstance(value, dict):
            self._events.put(value)


def _environment_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        for key in ("environment_id", "selected_environment_id", "id", "node_id"):
            selected = value.get(key)
            if isinstance(selected, str) and selected:
                return selected
        selected = value.get("selected")
        if selected is not value:
            return _environment_id(selected)
    return None


def _process_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in ("process_id", "session_id"):
            process_id = value.get(key)
            if isinstance(process_id, (str, int)) and str(process_id):
                return str(process_id)
        for key in ("result", "process", "session"):
            nested = value.get(key)
            process_id = _process_id(nested)
            if process_id:
                return process_id
    return None


def _output(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("output", "stdout", "text"):
            output = value.get(key)
            if isinstance(output, str):
                return output
        for key in ("result", "process", "session"):
            output = _output(value.get(key))
            if output:
                return output
    return ""


def _terminal(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("running") is False or value.get("done") is True:
        return True
    if value.get("exit_code") is not None or value.get("returncode") is not None:
        return True
    for key in ("result", "process", "session"):
        nested = value.get(key)
        if nested is not value and _terminal(nested):
            return True
    return False

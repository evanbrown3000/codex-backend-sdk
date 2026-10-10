"""Local and selected-environment transports for an unchanged Codex App Server."""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any, Callable, Mapping, Protocol, Sequence

from .environment_app_server import SelectedEnvironmentAppServer
from .remote_shell import RemoteShellClient


MessageHandler = Callable[[dict[str, Any]], None]
TextHandler = Callable[[str], None]


class AppServerTransport(Protocol):
    environment_id: str

    def send(self, message: Mapping[str, Any]) -> None: ...
    def close(self) -> None: ...


class LocalAppServerTransport:
    def __init__(
        self,
        command: Sequence[str],
        *,
        handler: MessageHandler,
        stderr_handler: TextHandler,
        environment_id: str = "local",
    ) -> None:
        self.environment_id = environment_id
        self.handler = handler
        self.stderr_handler = stderr_handler
        self.process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._write_lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self.handler(message)

    def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for line in self.process.stderr:
            self.stderr_handler(line)

    def send(self, message: Mapping[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError("native Codex App Server is not running")
        with self._write_lock:
            self.process.stdin.write(
                json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


class RemoteAppServerTransport:
    """Callback adapter over the single selected-environment implementation."""

    def __init__(
        self,
        relay: RemoteShellClient,
        environment_id: str,
        command: Sequence[str],
        *,
        handler: MessageHandler,
        stderr_handler: TextHandler,
    ) -> None:
        self.environment_id = environment_id
        self.stderr_handler = stderr_handler
        relay.select(environment_id)
        self.server = SelectedEnvironmentAppServer(
            relay,
            executable=command[0],
            arguments=command[1:],
            environment_id=environment_id,
        ).start()
        threading.Thread(
            target=self._read,
            args=(handler,),
            name="remote-app-server-events",
            daemon=True,
        ).start()

    def _read(self, handler: MessageHandler) -> None:
        try:
            for event in self.server.events():
                handler(event)
        except Exception as exc:
            self.stderr_handler(f"remote App Server stream: {type(exc).__name__}\n")

    def send(self, message: Mapping[str, Any]) -> None:
        self.server.send(message)

    def close(self) -> None:
        self.server.close()

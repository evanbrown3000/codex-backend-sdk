"""Launch B4PT0R Desktop with inherited central operator custody."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def _operator_capability() -> bytes:
    container = os.environ.get("COGNILODE_CLIENT_CONTAINER", "cognilode-primary-workspace")
    source = os.environ.get(
        "COGNILODE_CONTAINER_OPERATOR_TOKEN",
        "/opt/cognilode/runtime/control-secrets/operator-token",
    )
    result = subprocess.run(
        ["docker", "exec", container, "cat", source],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise SystemExit("Central operator custody is unavailable from the company container.")
    return result.stdout.strip()


def main() -> int:
    capability = _operator_capability()
    descriptor = os.memfd_create("cognilode-operator-capability", flags=0)
    os.write(descriptor, capability)
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.set_inheritable(descriptor, True)

    executable_dir = Path(sys.executable).resolve().parent
    bridge = executable_dir / "b4pt0r-app-server"
    if not bridge.is_file():
        located = shutil.which("b4pt0r-app-server")
        if not located:
            raise SystemExit("B4PT0R App Server bridge is not installed.")
        bridge = Path(located)

    app_image = Path(os.environ.get(
        "B4PT0R_DESKTOP_APPIMAGE",
        str(Path.home() / ".local/opt/b4pt0r/codex-desktop-linux_0.5.9_x86_64.AppImage"),
    )).expanduser()
    if not app_image.is_file():
        raise SystemExit(f"B4PT0R Desktop AppImage is missing: {app_image}")

    env = os.environ.copy()
    env.update({
        "APPIMAGE_EXTRACT_AND_RUN": "1",
        "CODEX_EXECUTABLE": str(bridge),
        "CODEX_NATIVE_EXECUTABLE": env.get(
            "CODEX_NATIVE_EXECUTABLE", str(Path.home() / ".local/bin/codex")
        ),
        "COGNILODE_OPERATOR_TOKEN_FILE": f"/proc/self/fd/{descriptor}",
        "COGNILODE_REMOTE_SHELL_ENDPOINT": env.get(
            "COGNILODE_REMOTE_SHELL_ENDPOINT",
            "https://cognilode.com/api/operator/remote-shell/mcp",
        ),
        "AGENT_MEMORY_ENDPOINT": env.get(
            "AGENT_MEMORY_ENDPOINT",
            "https://cognilode.com/api/operator/conversations",
        ),
    })
    os.execve(str(app_image), [str(app_image), *sys.argv[1:]], env)
    return 0

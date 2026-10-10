"""Launch B4PT0R Desktop with inherited central operator custody."""

from __future__ import annotations

import os
from pathlib import Path
import json
import shutil
import sys


def main() -> int:
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

    # The desktop receives an operation capability, never a copied operator or
    # provider credential.  The singular company custody container performs
    # Agent Memory, remote-shell, and queue-admission calls on its behalf.
    custody_container = os.environ.get(
        "COGNILODE_CLIENT_CONTAINER",
        "cognilode-company-runtime-chatmode-central-1",
    )
    broker_command = json.dumps([
        "docker", "exec", "-i", custody_container,
        "python3",
        "/runtime/source/current/codex-backend-sdk/scripts/b4pt0r-provider-broker",
    ])
    env = os.environ.copy()
    env.update({
        "APPIMAGE_EXTRACT_AND_RUN": "1",
        "CODEX_EXECUTABLE": str(bridge),
        "CODEX_NATIVE_EXECUTABLE": env.get(
            "CODEX_NATIVE_EXECUTABLE", str(Path.home() / ".local/bin/codex")
        ),
        "B4PT0R_PROVIDER_BROKER_COMMAND": env.get(
            "B4PT0R_PROVIDER_BROKER_COMMAND", broker_command
        ),
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

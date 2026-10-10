"""Metadata-only discovery of company ChatGPT credentials and requesters.

The inventory deliberately records credential *locations*, identities and
launch paths without serializing token values, cookies or browser databases.
Personal browser and Codex state on EvanPC are excluded by default.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Sequence


INVENTORY_SCHEMA = "cognilode.chatgpt_request_inventory.v1"
SECRET_ENV_NAMES = re.compile(
    r"(?:TOKEN|COOKIE|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL|SESSION|API_KEY)", re.I
)
REQUEST_MARKERS = (
    "chatgpt.com",
    "backend-api/conversation",
    "cognilode-b4pt0r-chatmode",
    "cognilode-b4pt0r-collect",
    "chatmode-queue-worker",
    "playwright",
    "chromedriver",
    "xvfb",
)
PERSONAL_BROWSER_MARKERS = (
    "/.config/google-chrome",
    "/.config/chromium",
    "/snap/chromium/",
)
HOST_EXCEPTIONS = (
    "evan-recorder",
    "cognilode-company-containers.service",
    "codex-desktop-b4pt0r",
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_command(command: str) -> str:
    command = re.sub(
        r"(?i)(token|cookie|secret|password|passwd|authorization|api[_-]?key)=([^\s]+)",
        r"\1=<redacted>",
        command,
    )
    command = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", command)
    return command[:4096]


def _company_requester(command: str, environment_keys: Iterable[str]) -> bool:
    lowered = command.lower()
    return any(marker in lowered for marker in REQUEST_MARKERS) or any(
        key.startswith("COGNILODE_") and "CHAT" in key for key in environment_keys
    )


@dataclass(frozen=True)
class CredentialLocation:
    environment_id: str
    kind: str
    path: str
    owner_uid: int
    mode: str
    size: int
    fingerprint: str
    personal_exclusion: bool


@dataclass(frozen=True)
class Requester:
    environment_id: str
    source: str
    identity: str
    command: str
    credential_env_names: tuple[str, ...]
    credential_paths: tuple[str, ...]
    launch_path: str | None
    active: bool
    host_exception: bool


@dataclass(frozen=True)
class Inventory:
    schema: str
    environment_id: str
    environment_kind: str
    observed_at: str
    credentials: tuple[CredentialLocation, ...]
    requesters: tuple[Requester, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "credentials": [asdict(item) for item in self.credentials],
            "requesters": [asdict(item) for item in self.requesters],
        }


def _personal_path(path: Path, *, evanpc_host: bool) -> bool:
    text = path.as_posix().lower()
    if not evanpc_host:
        return False
    return any(marker in text for marker in PERSONAL_BROWSER_MARKERS) or text.endswith(
        "/.codex/auth.json"
    )


def discover_credentials(
    roots: Iterable[Path], *, environment_id: str, evanpc_host: bool
) -> tuple[CredentialLocation, ...]:
    candidates: set[Path] = set()
    exact_names = {"auth.json", "cookies", "cookies.sqlite", "local state", "login data"}
    for root in roots:
        root = root.expanduser()
        if root.is_file():
            candidates.add(root)
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and (
                path.name.lower() in exact_names
                or SECRET_ENV_NAMES.search(path.name)
                or path.suffix.lower() in {".cookie", ".cookies"}
            ):
                candidates.add(path)
    result: list[CredentialLocation] = []
    for path in sorted(candidates):
        personal = _personal_path(path, evanpc_host=evanpc_host)
        stat = path.stat()
        kind = "codex_auth" if path.name == "auth.json" else (
            "browser_state" if path.name.lower() in {"cookies", "cookies.sqlite", "local state", "login data"}
            else "credential_file"
        )
        result.append(
            CredentialLocation(
                environment_id=environment_id,
                kind=kind,
                path=str(path.resolve()),
                owner_uid=stat.st_uid,
                mode=oct(stat.st_mode & 0o777),
                size=stat.st_size,
                fingerprint=_fingerprint_file(path),
                personal_exclusion=personal,
            )
        )
    return tuple(result)


def discover_processes(
    *, environment_id: str, evanpc_host: bool, proc_root: Path = Path("/proc")
) -> tuple[Requester, ...]:
    found: list[Requester] = []
    try:
        processes = sorted((path for path in proc_root.iterdir() if path.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        return ()
    for process in processes:
        try:
            command = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            raw_environment = (process / "environ").read_bytes().split(b"\0")
            environment: dict[str, str] = {}
            for value in raw_environment:
                key, separator, item = value.partition(b"=")
                if separator:
                    environment[key.decode(errors="replace")] = item.decode(errors="replace")
        except (OSError, PermissionError):
            continue
        if not _company_requester(command, environment):
            continue
        credential_keys = tuple(sorted(key for key in environment if SECRET_ENV_NAMES.search(key)))
        credential_paths = tuple(
            sorted(
                value
                for key, value in environment.items()
                if (SECRET_ENV_NAMES.search(key) or key in {"CODEX_HOME", "CHROME_USER_DATA_DIR"})
                and value.startswith("/")
            )
        )
        personal = evanpc_host and any(marker in command.lower() for marker in PERSONAL_BROWSER_MARKERS)
        host_exception = personal or any(marker in command for marker in HOST_EXCEPTIONS)
        launch_path = None
        try:
            launch_path = str((process / "exe").resolve(strict=True))
        except OSError:
            pass
        found.append(
            Requester(
                environment_id=environment_id,
                source="process",
                identity=process.name,
                command=_redact_command(command),
                credential_env_names=credential_keys,
                credential_paths=credential_paths,
                launch_path=launch_path,
                active=True,
                host_exception=host_exception,
            )
        )
    return tuple(found)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False, timeout=45
    )


def discover_user_units(*, environment_id: str) -> tuple[Requester, ...]:
    listed = _systemctl("list-unit-files", "--no-legend", "--plain")
    requesters: list[Requester] = []
    for line in listed.stdout.splitlines():
        parts = line.split()
        if not parts or not parts[0].endswith((".service", ".timer", ".socket")):
            continue
        name = parts[0]
        shown = _systemctl("show", name, "--property=ExecStart,FragmentPath,Environment,ActiveState")
        properties = dict(
            value.split("=", 1) for value in shown.stdout.splitlines() if "=" in value
        )
        command = properties.get("ExecStart", "")
        env_text = properties.get("Environment", "")
        env_names = tuple(
            sorted(
                match.group(1)
                for match in re.finditer(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", env_text)
                if SECRET_ENV_NAMES.search(match.group(1))
            )
        )
        if not _company_requester(command + " " + name, env_names):
            continue
        requesters.append(
            Requester(
                environment_id=environment_id,
                source="systemd_user",
                identity=name,
                command=_redact_command(command),
                credential_env_names=env_names,
                credential_paths=(),
                launch_path=properties.get("FragmentPath") or None,
                active=properties.get("ActiveState") == "active",
                host_exception=name in HOST_EXCEPTIONS or "evan-recorder" in name,
            )
        )
    return tuple(requesters)


def discover_docker(*, environment_id: str) -> tuple[Requester, ...]:
    listed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    if listed.returncode != 0:
        return ()
    requesters: list[Requester] = []
    for container_id in listed.stdout.split():
        inspected = subprocess.run(
            ["docker", "inspect", container_id], capture_output=True, text=True, check=False, timeout=45
        )
        if inspected.returncode != 0:
            continue
        values = json.loads(inspected.stdout)
        if not values:
            continue
        value = values[0]
        config = value.get("Config") or {}
        command = " ".join(
            str(part) for part in [config.get("Entrypoint") or "", config.get("Cmd") or ""]
        )
        environment = config.get("Env") or []
        env_names = tuple(
            sorted(
                item.split("=", 1)[0]
                for item in environment
                if "=" in item and SECRET_ENV_NAMES.search(item.split("=", 1)[0])
            )
        )
        name = str(value.get("Name") or container_id).lstrip("/")
        if not _company_requester(command + " " + name, env_names):
            continue
        paths = tuple(
            sorted(
                str(mount.get("Source"))
                for mount in value.get("Mounts") or []
                if mount.get("Source") and re.search(r"auth|credential|browser|codex", str(mount.get("Destination")), re.I)
            )
        )
        requesters.append(
            Requester(
                environment_id=environment_id,
                source="docker",
                identity=name,
                command=_redact_command(command),
                credential_env_names=env_names,
                credential_paths=paths,
                launch_path=str((config.get("Labels") or {}).get("com.docker.compose.project.config_files") or "") or None,
                active=bool((value.get("State") or {}).get("Running")),
                host_exception=False,
            )
        )
    return tuple(requesters)


def discover_github_actions(*, environment_id: str, owner: str) -> tuple[Requester, ...]:
    """Discover active Actions paths capable of bypassing the provider broker.

    GitHub exposes secret names but never their values.  The returned records
    connect each workflow to only the secret names it actually references.
    """

    repos = subprocess.run(
        ["gh", "repo", "list", owner, "--limit", "1000", "--json", "nameWithOwner"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if repos.returncode != 0:
        return ()
    requesters: list[Requester] = []
    for repository in json.loads(repos.stdout):
        name = str(repository.get("nameWithOwner") or "")
        if not name:
            continue
        workflows = subprocess.run(
            ["gh", "workflow", "list", "--all", "--repo", name, "--json", "name,path,state"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if workflows.returncode != 0:
            continue
        for workflow in json.loads(workflows.stdout):
            path = str(workflow.get("path") or "")
            if not path:
                continue
            content = subprocess.run(
                ["gh", "api", f"repos/{name}/contents/{path}", "--jq", ".content"],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if content.returncode != 0:
                continue
            try:
                import base64

                source = base64.b64decode("".join(content.stdout.split())).decode(errors="replace")
            except (ValueError, TypeError):
                continue
            if not _company_requester(source + " " + path, ()):
                continue
            secret_names = tuple(
                sorted(set(re.findall(r"\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)", source)))
            )
            trigger_lines = " ".join(
                line.strip()
                for line in source.splitlines()
                if re.search(r"schedule:|cron:|workflow_dispatch:|repository_dispatch:|chatgpt|b4pt0r|conversation", line, re.I)
            )
            requesters.append(
                Requester(
                    environment_id=environment_id,
                    source="github_actions",
                    identity=f"{name}:{path}",
                    command=_redact_command(trigger_lines),
                    credential_env_names=secret_names,
                    credential_paths=(),
                    launch_path=f"https://github.com/{name}/blob/HEAD/{path}",
                    active=str(workflow.get("state") or "").lower() == "active",
                    host_exception=False,
                )
            )
    return tuple(requesters)


def discover_command_collectors(
    commands: Iterable[Sequence[str]], *, environment_id: str
) -> tuple[Requester, ...]:
    """Merge provider-specific inventories without embedding another API.

    Cloudflare, AWS and OpenAI Library collectors can run beside their own
    credentials.  They return metadata-only ``Requester`` dictionaries using
    this module's stable schema.
    """

    requesters: list[Requester] = []
    request = json.dumps(
        {"operation": "credential_request_inventory", "environment_id": environment_id},
        separators=(",", ":"),
    )
    for command in commands:
        completed = subprocess.run(
            list(command),
            input=request + "\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if completed.returncode != 0:
            continue
        try:
            value = json.loads(completed.stdout)
            for item in value.get("requesters", ()):
                requesters.append(Requester(**item))
        except (ValueError, TypeError, KeyError):
            continue
    return tuple(requesters)


def inventory_environment(
    *,
    environment_id: str,
    environment_kind: str,
    credential_roots: Sequence[Path],
    include_systemd: bool = True,
    include_docker: bool = True,
    github_owner: str | None = None,
    collector_commands: Iterable[Sequence[str]] = (),
) -> Inventory:
    evanpc_host = environment_kind == "evanpc_host"
    requesters = list(discover_processes(environment_id=environment_id, evanpc_host=evanpc_host))
    if include_systemd:
        requesters.extend(discover_user_units(environment_id=environment_id))
    if include_docker:
        requesters.extend(discover_docker(environment_id=environment_id))
    if github_owner:
        requesters.extend(
            discover_github_actions(environment_id=environment_id, owner=github_owner)
        )
    requesters.extend(
        discover_command_collectors(collector_commands, environment_id=environment_id)
    )
    unique = {(item.source, item.identity): item for item in requesters}
    return Inventory(
        schema=INVENTORY_SCHEMA,
        environment_id=environment_id,
        environment_kind=environment_kind,
        observed_at=_utc(),
        credentials=discover_credentials(
            credential_roots, environment_id=environment_id, evanpc_host=evanpc_host
        ),
        requesters=tuple(sorted(unique.values(), key=lambda item: (item.source, item.identity))),
    )

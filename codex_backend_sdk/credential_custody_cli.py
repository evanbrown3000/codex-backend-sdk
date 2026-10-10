"""CLI for encrypted custody, broker activation and requester containment."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
from typing import Any

from .credential_custody import (
    BrokerLease,
    CommandStorage,
    CredentialCustody,
    CustodyError,
    load_encryption_key,
)
from .credential_inventory import Inventory, inventory_environment


def _storage(value: str) -> CommandStorage:
    command = json.loads(value)
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError("storage command must be a JSON argv array")
    return CommandStorage(command)


def _custody(args: argparse.Namespace) -> CredentialCustody:
    return CredentialCustody(
        _storage(args.storage_command),
        bundle_locator=args.bundle_locator,
        lease_locator=args.lease_locator,
        key=load_encryption_key(args.key_file),
    )


def _write_json(value: Any, destination: Path | None = None) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    if destination is None:
        sys.stdout.write(payload)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)


def _publish_json(storage: CommandStorage, locator: str, payload: dict[str, Any]) -> str:
    with tempfile.TemporaryDirectory(prefix="custody-registry-") as directory:
        source = Path(directory) / "registry.json"
        _write_json(payload, source)
        current = storage.stat(locator)
        stored = storage.write(
            locator,
            source,
            expected_revision=current.revision if current else None,
        )
        return stored.revision


def command_inventory(args: argparse.Namespace) -> int:
    collector_commands = tuple(json.loads(value) for value in args.collector_command)
    if any(
        not isinstance(command, list) or not all(isinstance(part, str) for part in command)
        for command in collector_commands
    ):
        raise ValueError("collector commands must be JSON argv arrays")
    inventory = inventory_environment(
        environment_id=args.environment,
        environment_kind=args.environment_kind,
        credential_roots=tuple(args.credential_root),
        include_systemd=not args.no_systemd,
        include_docker=not args.no_docker,
        github_owner=args.github_owner,
        collector_commands=collector_commands,
    )
    value = inventory.to_dict()
    if args.registry_locator:
        value["registry_revision"] = _publish_json(
            _storage(args.storage_command), args.registry_locator, value
        )
    _write_json(value, args.output)
    return 0


def _entry(value: str) -> tuple[str, str, Path]:
    try:
        name, kind, path = value.split(":", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("entry must be NAME:KIND:PATH") from exc
    if not name or not kind or not path:
        raise argparse.ArgumentTypeError("entry must be NAME:KIND:PATH")
    return name, kind, Path(path)


def command_move(args: argparse.Namespace) -> int:
    if args.environment_kind == "evanpc_host":
        for _, kind, path in args.entry:
            lowered = path.expanduser().as_posix().lower()
            if kind == "personal_browser" or "/.config/google-chrome" in lowered or "/.config/chromium" in lowered:
                raise CustodyError("personal EvanPC browser state is outside company custody")
    custody = _custody(args)
    current = custody.storage.stat(custody.bundle_locator)
    stored = custody.move_into_custody(
        args.entry,
        source_environment=args.environment,
        expected_revision=current.revision if current else None,
        remove_sources=True,
    )
    _write_json(
        {
            "ok": True,
            "operation": "moved_into_custody",
            "revision": stored.revision,
            "source_count": len(args.entry),
        }
    )
    return 0


class _LeaseRenewer:
    def __init__(self, custody: CredentialCustody, lease: BrokerLease, ttl: int) -> None:
        self.custody = custody
        self.lease = lease
        self.ttl = ttl
        self.failure: BaseException | None = None
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(max(10, self.ttl // 3)):
            try:
                self.lease = self.custody.renew_lease(self.lease, ttl_seconds=self.ttl)
            except BaseException as exc:
                self.failure = exc
                self.stop.set()
                return


def _materialize_expected_files(runtime: Path, manifest: Any) -> dict[str, str]:
    environment: dict[str, str] = {}
    for entry in manifest.entries:
        source = runtime / "bundle" / entry.archive_path
        if entry.kind == "codex_auth":
            codex_home = runtime / "codex"
            codex_home.mkdir(mode=0o700, exist_ok=True)
            destination = codex_home / "auth.json"
            shutil.copy2(source, destination)
            os.chmod(destination, 0o600)
            environment["CODEX_HOME"] = str(codex_home)
        elif entry.kind == "browser_profile":
            destination = runtime / "browser-profile"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            environment["COGNILODE_CHROME_PROFILE"] = str(destination)
    if "CODEX_HOME" not in environment and "COGNILODE_CHROME_PROFILE" not in environment:
        raise CustodyError("credential bundle contains no provider authentication material")
    return environment


def command_broker(args: argparse.Namespace) -> int:
    runtime = args.runtime.expanduser().resolve()
    if runtime == Path("/") or runtime == Path.home():
        raise CustodyError("broker runtime must be an isolated directory")
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True, mode=0o700)
    gate_socket = args.http_gate_socket.expanduser().resolve()
    if not gate_socket.exists():
        raise CustodyError("provider broker requires the central HTTP gate socket")
    custody = _custody(args)
    lease = custody.acquire_lease(
        broker_id=args.broker_id,
        environment_id=args.environment,
        ttl_seconds=args.lease_ttl,
    )
    manifest, stored = custody.materialize(runtime / "bundle")
    provider_environment = _materialize_expected_files(runtime, manifest)
    renewer = _LeaseRenewer(custody, lease, args.lease_ttl)
    thread = threading.Thread(target=renewer.run, name="credential-lease-renewer", daemon=True)
    thread.start()
    environment = {
        **os.environ,
        **provider_environment,
        "COGNILODE_CREDENTIAL_BROKER_ID": args.broker_id,
        "COGNILODE_CREDENTIAL_LEASE_GENERATION": str(lease.generation),
        "COGNILODE_CREDENTIAL_BUNDLE_REVISION": stored.revision,
        "COGNILODE_HTTP_GATE_SOCKET": str(gate_socket),
        "COGNILODE_HTTP_SOURCE": f"credential-broker:{args.broker_id}",
    }
    if args.http_gate_pythonpath:
        prior = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(args.http_gate_pythonpath) + (os.pathsep + prior if prior else "")
    child = subprocess.Popen(args.provider_command, env=environment)
    try:
        while child.poll() is None:
            if renewer.failure:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                raise CustodyError("provider broker lost credential custody lease") from renewer.failure
            time.sleep(1)
        return int(child.returncode or 0)
    finally:
        renewer.stop.set()
        thread.join(timeout=5)
        shutil.rmtree(runtime, ignore_errors=True)


def _load_inventory(path: Path) -> Inventory:
    from .credential_inventory import CredentialLocation, Requester

    value = json.loads(path.read_text(encoding="utf-8"))
    return Inventory(
        schema=value["schema"],
        environment_id=value["environment_id"],
        environment_kind=value["environment_kind"],
        observed_at=value["observed_at"],
        credentials=tuple(CredentialLocation(**item) for item in value.get("credentials", ())),
        requesters=tuple(Requester(**item) for item in value.get("requesters", ())),
    )


def command_contain(args: argparse.Namespace) -> int:
    storage = _storage(args.storage_command)
    bundle = storage.stat(args.bundle_locator)
    lease_object = storage.stat(args.lease_locator)
    if bundle is None or lease_object is None:
        raise CustodyError("central bundle and active broker lease are required before containment")
    with tempfile.TemporaryDirectory(prefix="custody-containment-") as directory:
        lease_path = Path(directory) / "lease.json"
        observed = storage.read(args.lease_locator, lease_path)
        if observed.revision != lease_object.revision:
            raise CustodyError("broker lease changed during containment admission")
        lease = BrokerLease(**json.loads(lease_path.read_text(encoding="utf-8")))
    if lease.broker_id != args.broker_id or lease.expires_at <= time.time():
        raise CustodyError("the selected credential broker does not hold an active lease")
    inventories = tuple(_load_inventory(path) for path in args.inventory)
    authorized = set(args.authorized_requester)
    actions: list[dict[str, Any]] = []
    for inventory in inventories:
        for requester in inventory.requesters:
            if not requester.active or requester.host_exception or requester.identity in authorized:
                continue
            if requester.source == "systemd_user":
                command = ["systemctl", "--user", "disable", "--now", requester.identity]
            elif requester.source == "docker":
                command = ["docker", "stop", requester.identity]
            elif requester.source == "process" and requester.identity.isdigit():
                command = ["kill", "-TERM", requester.identity]
            elif requester.source == "github_actions" and ":" in requester.identity:
                repository, workflow = requester.identity.split(":", 1)
                command = ["gh", "workflow", "disable", workflow, "--repo", repository]
            else:
                continue
            completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=90)
            actions.append(
                {
                    "environment_id": inventory.environment_id,
                    "source": requester.source,
                    "identity": requester.identity,
                    "action": command[:2],
                    "applied": completed.returncode == 0,
                }
            )
    _write_json({"ok": all(item["applied"] for item in actions), "actions": actions})
    return 0 if all(item["applied"] for item in actions) else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory")
    inventory.add_argument("--environment", required=True)
    inventory.add_argument(
        "--environment-kind",
        choices=("evanpc_host", "container", "ec2", "other"),
        required=True,
    )
    inventory.add_argument("--credential-root", action="append", type=Path, default=[])
    inventory.add_argument("--no-systemd", action="store_true")
    inventory.add_argument("--no-docker", action="store_true")
    inventory.add_argument("--github-owner")
    inventory.add_argument(
        "--collector-command",
        action="append",
        default=[],
        help="JSON argv for a provider-specific metadata collector",
    )
    inventory.add_argument("--output", type=Path)
    inventory.add_argument("--registry-locator")
    inventory.add_argument("--storage-command", help="JSON argv for the unified storage CLI")
    inventory.set_defaults(handler=command_inventory)

    def custody_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--storage-command", required=True)
        command.add_argument("--bundle-locator", required=True)
        command.add_argument("--lease-locator", required=True)
        command.add_argument("--key-file", required=True, type=Path)

    move = commands.add_parser("move")
    custody_arguments(move)
    move.add_argument("--environment", required=True)
    move.add_argument("--environment-kind", required=True)
    move.add_argument("--entry", action="append", type=_entry, required=True)
    move.set_defaults(handler=command_move)

    broker = commands.add_parser("broker")
    custody_arguments(broker)
    broker.add_argument("--broker-id", required=True)
    broker.add_argument("--environment", required=True)
    broker.add_argument("--runtime", required=True, type=Path)
    broker.add_argument("--http-gate-socket", required=True, type=Path)
    broker.add_argument("--http-gate-pythonpath", type=Path)
    broker.add_argument("--lease-ttl", type=int, default=90)
    broker.add_argument("provider_command", nargs=argparse.REMAINDER)
    broker.set_defaults(handler=command_broker)

    contain = commands.add_parser("contain")
    contain.add_argument("--storage-command", required=True)
    contain.add_argument("--bundle-locator", required=True)
    contain.add_argument("--lease-locator", required=True)
    contain.add_argument("--broker-id", required=True)
    contain.add_argument("--inventory", action="append", type=Path, required=True)
    contain.add_argument("--authorized-requester", action="append", default=[])
    contain.set_defaults(handler=command_contain)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "inventory" and args.registry_locator and not args.storage_command:
        raise SystemExit("--storage-command is required with --registry-locator")
    if args.command == "broker" and not args.provider_command:
        raise SystemExit("provider command is required")
    try:
        return int(args.handler(args))
    except (CustodyError, OSError, ValueError, json.JSONDecodeError) as exc:
        _write_json({"ok": False, "error": type(exc).__name__, "message": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

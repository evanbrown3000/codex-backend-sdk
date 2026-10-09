from __future__ import annotations

import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-install-chatmode-termux"


def fake_phone(tmp_path: Path) -> dict[str, str]:
    prefix = tmp_path / "data/data/com.termux/files/usr"
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("pkg", "proot-distro", "sv-enable", "sv"):
        command = commands / name
        command.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "$COGNILODE_TEST_CALLS"\n')
        command.chmod(0o700)
    return {**os.environ, "PREFIX": str(prefix), "HOME": str(tmp_path / "home"),
            "PATH": str(commands) + ":" + os.environ["PATH"],
            "COGNILODE_TEST_CALLS": str(tmp_path / "calls")}


def run_phone(env: dict[str, str], action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), action], env=env, text=True,
                          capture_output=True, check=False)


def test_prepare_writes_low_priority_durable_runner_but_does_not_activate(tmp_path):
    env = fake_phone(tmp_path)
    result = run_phone(env, "prepare")
    assert result.returncode == 0, result.stderr
    runner = Path(env["HOME"]) / ".local/bin/cognilode-chatmode-phone-run"
    assert "nice -n 15 proot-distro login ubuntu" in runner.read_text()
    assert "--device-id \"home-android-phone\" --interval 60" in runner.read_text()
    assert (Path(env["PREFIX"]) / "var/service/cognilode-chatmode-phone/down").is_file()
    assert (Path(env["HOME"]) / ".termux/boot/20-cognilode-chatmode").is_file()
    assert "sv-enable" not in Path(env["COGNILODE_TEST_CALLS"]).read_text()


def test_activate_requires_credentials_and_private_zip_probe(tmp_path):
    env = fake_phone(tmp_path)
    result = run_phone(env, "activate")
    assert result.returncode == 3
    assert "operator-bearer" in result.stderr
    assert not Path(env["COGNILODE_TEST_CALLS"]).exists()


def test_activate_verifies_before_service_enable(tmp_path):
    env = fake_phone(tmp_path)
    home = Path(env["HOME"])
    bearer = home / ".config/cognilode/operator-bearer"
    bearer.parent.mkdir(parents=True)
    bearer.write_text("test-bearer")
    bearer.chmod(0o600)
    credentials = home / ".aws/credentials"
    credentials.parent.mkdir()
    credentials.write_text("[default]\naws_access_key_id=test\n")
    credentials.chmod(0o600)
    env["COGNILODE_PHONE_PROBE_SHA256"] = "a" * 64
    result = run_phone(env, "activate")
    assert result.returncode == 0, result.stderr
    calls = Path(env["COGNILODE_TEST_CALLS"]).read_text().splitlines()
    assert calls[0].startswith("proot-distro login ubuntu")
    assert calls[1].startswith("proot-distro login ubuntu")
    assert calls[2] == "sv-enable cognilode-chatmode-phone"

from __future__ import annotations

import argparse
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-install-chatmode-device"


def installer():
    loader = SourceFileLoader("chatmode_device_installer_test", str(SCRIPT))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_browser_authenticated_second_device_enrolls_without_copying_credentials(monkeypatch, tmp_path):
    module = installer()
    profile = tmp_path / "Chrome Profile"
    profile.mkdir()
    aws = tmp_path / "aws"
    aws.write_text("#!/bin/sh\nexit 0\n")
    aws.chmod(0o700)
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[-1] == "health":
            return '{"ok":true}'
        if argv[0] == "loginctl" and "show-user" in argv:
            return "yes\n"
        return ""

    class Sender:
        @staticmethod
        def operator_memory_post(_body):
            return {"ok": True, "initialized": True, "gate": {"devices": [
                {"id": "home-laptop", "available": True}]}}

    monkeypatch.setattr(module, "_run", run)
    monkeypatch.setattr(module, "_load", lambda *_args: Sender())
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    args = argparse.Namespace(device_id="home-laptop", priority=60, network_route="home",
                              auth_source="chrome", chrome_profile=profile,
                              aws_cli=aws, bucket="private-test-bucket", probe_sha256="")
    result = module.install(args)
    assert result["central_heartbeat_readback"] is True
    assert calls[0][1:] == ["--auth-source", "chrome", "--chrome-profile", str(profile), "health"]
    config = (tmp_path / ".config/cognilode/chatmode-queue-worker.env").read_text()
    assert "COGNILODE_CHATMODE_AUTH_SOURCE=chrome\n" in config
    assert f"COGNILODE_CHATMODE_CHROME_PROFILE={profile}\n" in config
    assert "access_token" not in config
    assert any(argv[0] == "loginctl" and "show-user" in argv for argv in calls)

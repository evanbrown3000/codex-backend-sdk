from __future__ import annotations

import hashlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-enroll-phone-over-ssh"


def enroler():
    loader = SourceFileLoader("phone_enroll_test", str(SCRIPT))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_remote_enrollment_checks_android_and_stages_credentials_without_eligibility(monkeypatch, tmp_path):
    module = enroler()
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    (tmp_path / ".config/cognilode").mkdir(parents=True)
    (tmp_path / ".aws").mkdir()
    for name in (".config/cognilode/operator-bearer", ".aws/credentials", ".aws/config"):
        p = tmp_path / name
        p.write_text("private")
        p.chmod(0o600)
    calls = []

    def fake_run(argv, *, payload=None, timeout=30):
        calls.append((argv, payload, timeout))
        if "ro.build.fingerprint" in argv[-1]:
            return "google/pixel/phone\n"
        if "$PREFIX" in argv[-1]:
            return "/data/data/com.termux/files/usr"
        if argv[-1].startswith("sha256sum "):
            return hashlib.sha256(b"private").hexdigest() + "  file\n"
        return ""

    monkeypatch.setattr(module, "run", fake_run)
    result = module.enroll("192.168.10.198", "u0_a123", 8022, "a" * 64, prepare=True)
    assert result["android_fingerprint_present"] is True
    assert result["credentials_staged_over_ssh"] is True
    assert result["d1_eligible"] is False
    assert result["chatgpt_send_performed"] is False
    assert len([payload for _, payload, _ in calls if payload == b"private"]) == 3
    assert len([argv for argv, _, _ in calls if argv[-1].startswith("sha256sum ")]) == 3
    assert any("cognilode-install-chatmode-termux prepare" in argv[-1] for argv, _, _ in calls)


def test_remote_enrollment_rejects_non_termux_target(monkeypatch):
    module = enroler()
    monkeypatch.setattr(module, "run", lambda argv, **_: "linux-host" if "$PREFIX" in argv[-1] else "build")
    with pytest.raises(RuntimeError, match="not the Termux"):
        module.enroll("192.168.10.198", "u0_a123", 8022, "a" * 64, prepare=False)


def test_remote_enrollment_rejects_credential_mismatch_before_prepare(monkeypatch, tmp_path):
    module = enroler()
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    (tmp_path / ".config/cognilode").mkdir(parents=True)
    (tmp_path / ".aws").mkdir()
    for name in (".config/cognilode/operator-bearer", ".aws/credentials", ".aws/config"):
        p = tmp_path / name
        p.write_text("private")
        p.chmod(0o600)
    calls = []

    def fake_run(argv, **_):
        calls.append(argv[-1])
        if "ro.build.fingerprint" in argv[-1]: return "android/device"
        if "$PREFIX" in argv[-1]: return "/data/data/com.termux/files/usr"
        if argv[-1].startswith("sha256sum "): return "0" * 64 + "  file"
        return ""

    monkeypatch.setattr(module, "run", fake_run)
    with pytest.raises(RuntimeError, match="readback SHA mismatch"):
        module.enroll("Android.local", "u0_a123", 8022, "a" * 64, prepare=True)
    assert not any("cognilode-install-chatmode-termux prepare" in call for call in calls)

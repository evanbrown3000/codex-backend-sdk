from __future__ import annotations

from hashlib import sha256
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
from unittest import mock
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-taskflow-phase-controller"
loader = importlib.machinery.SourceFileLoader("root_probe_taskflow_test", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
taskflow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = taskflow
loader.exec_module(taskflow)


def packet_zip(path: Path, plan, step, script: bytes) -> None:
    member = "probes/A.py"
    target = str(Path.home() / ".local/share/cognilode/taskflow-probes/root-work/A.py")
    packet = {"schema": "cognilode.taskflow.independent_probe.v1",
              "plan_sha256": plan.sha256, "step_id": step.step_id,
              "command": step.fields["effect_probe_command"],
              "target_path": target, "source_member": member,
              "source_sha256": sha256(script).hexdigest()}
    contents = {"PROBE_INSTALL.json": json.dumps(packet, sort_keys=True).encode(),
                member: script,
                "EXTERNAL_EFFECT_INSTRUCTIONS.md": b"Install the frozen probe and deploy the work.\n"}
    manifest = {"plan_sha256": plan.sha256,
                "files": [{"path": name, "sha256": sha256(raw).hexdigest()}
                          for name, raw in contents.items()]}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("MANIFEST.json", json.dumps(manifest))
        for name, raw in contents.items():
            archive.writestr(name, raw)


def test_named_codex_probe_is_frozen_before_chatgpt_and_checked_after_install(tmp_path):
    with mock.patch.object(Path, "home", return_value=tmp_path):
        target = tmp_path / ".local/share/cognilode/taskflow-probes/root-work/A.py"
        beneficiary = tmp_path / "beneficiary.txt"
        beneficiary.write_bytes(b"actual deployed beneficiary state")
        observed = json.dumps({"schema": "cognilode.effect_probe.v1", "verified": True,
                               "evidence": [{"kind": "file", "ref": "file:" + str(beneficiary),
                                             "sha256": sha256(beneficiary.read_bytes()).hexdigest()}]})
        plan = taskflow.parse_plan_text(
            "project Root Work\nid root-work\nroot_conversation_id chat-1\n"
            "[ ] A Beneficiary effect\n"
            "    owner: Eli Rowan\n    hierarchy_scope: employee-eli\n"
            f"    effect_probe_command: /usr/bin/python3 -I {target}\n"
            "    effect_probe_expected: \n")
        step = plan.steps[0]
        script = ("import json\nprint(" + repr(observed) + ")\n").encode()
        research = tmp_path / "research.zip"
        work = tmp_path / "work.zip"
        packet_zip(research, plan, step, script)
        packet_zip(work, plan, step, script)
        taskflow.verify_manifest_zip(research, expected_plan_sha256=plan.sha256)
        taskflow.verify_manifest_zip(work, expected_plan_sha256=plan.sha256,
                                     require_external_instructions=True)
        assert taskflow.root_portfolio_plan(plan)
        assert "independently author" in taskflow.research_prompt(plan, step, "Nadia Brooks", research)
        assert "byte-for-byte" in taskflow.chatgpt_prompt(plan, step, {"sha256": "a" * 64})
        frozen = taskflow.verify_carried_independent_probe(research, work, plan, step)
        job = {"taskflow_step": "A", "conversation_id": "chat-1",
               "attachment_refs": [{"ref": "file:" + str(research),
                                    "sha256": sha256(research.read_bytes()).hexdigest()}],
               "effect_evidence": [
                   {"kind": "chatgpt_sandbox_artifact", "ref": sha256(work.read_bytes()).hexdigest(),
                    "path": str(work)},
                   {"kind": "provider_conversation", "ref": "chat-1"},
                   {"kind": "central_conversation_readback", "ref": "chat-1"},
                   {"kind": "provider_observed_native_exec", "ref": "native-call",
                    "call_ref": "native-result", "raw_stream_sha256": "f" * 64}]}
        assert taskflow.verified_chat_completion(job, plan)["sha256"] == sha256(work.read_bytes()).hexdigest()
        with pytest.raises(ValueError, match="not installed"):
            taskflow.verify_installed_independent_probe(work, plan, step)
        target.parent.mkdir(parents=True)
        target.write_bytes(script)
        assert taskflow.verify_installed_independent_probe(work, plan, step) == frozen
        assert taskflow.validate_probe_observation(step, observed) == "cognilode.effect_probe.v1 verified"
        with mock.patch.object(taskflow.subprocess, "run",
                               return_value=taskflow.subprocess.CompletedProcess([], 0, observed)) as run:
            taskflow.verify_effect_probe(step)
            assert run.call_args.args[0][:3] == ["/usr/bin/bwrap", "--die-with-parent", "--ro-bind"]
        beneficiary.write_bytes(b"changed beneficiary state")
        with pytest.raises(ValueError, match="changed since independent probe"):
            taskflow.validate_probe_observation(step, observed)
        target.write_bytes(b"print('fake success')\n")
        with pytest.raises(ValueError, match="not installed"):
            taskflow.verify_installed_independent_probe(work, plan, step)
        packet_zip(work, plan, step, b"print('changed by worker: would self-certify')\n")
        with pytest.raises(ValueError, match="changed independent research probe"):
            taskflow.verify_carried_independent_probe(research, work, plan, step)
        job["effect_evidence"][0]["ref"] = sha256(work.read_bytes()).hexdigest()
        with pytest.raises(ValueError, match="changed independent research probe"):
            taskflow.verified_chat_completion(job, plan)


def test_root_https_beneficiary_is_refetched_and_private_hosts_are_rejected():
    raw = b"actual public beneficiary payload"
    row = {"kind": "https", "ref": "https://beneficiary.example/status",
           "sha256": sha256(raw).hexdigest()}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def geturl(self): return row["ref"]
        def read(self, _limit): return raw
    with mock.patch.object(taskflow.socket, "getaddrinfo",
                           return_value=[(None, None, None, None, ("93.184.216.34", 443))]), \
         mock.patch.object(taskflow.urllib.request, "urlopen", return_value=Response()) as opened:
        taskflow.verify_root_beneficiary_ref(row)
        assert opened.call_args.args[0].get_method() == "GET"
    bad = dict(row, ref="https://localhost/private")
    with pytest.raises(ValueError, match="invalid"):
        taskflow.verify_root_beneficiary_ref(bad)

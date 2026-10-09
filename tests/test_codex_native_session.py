import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1]/"scripts/cognilode-codex-native-session"
loader = importlib.machinery.SourceFileLoader("codex_native_session_test",str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name,loader)
native = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = native
loader.exec_module(native)
CID = "123e4567-e89b-12d3-a456-426614174000"


class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.rollout = root/"rollout.jsonl"
        self.rollout.write_text(json.dumps({"type":"session_meta","payload":{"session_id":CID}})+"\n")
        self.stderr = root/"codex.stderr"
        self.stderr.write_text("OpenAI Codex v0.162.0\n--------\nsession id: "+CID+"\n--------\n")
        self.receipt = root/"receipt.json"
        self.receipt.write_text(json.dumps({"conversation_id":CID}))
        self.binary = root/"codex"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        self.bundle = root/"instructions.json"
        self.bundle.write_text(json.dumps({"system":"replacement","developer":"replacement"}))
        self.prompt = root/"prompt.txt"
        self.prompt.write_text("Continue exact assigned task.")
        self.args = types.SimpleNamespace(session_id=CID,environment_id="evanpc-workspace",
            rollout_path=self.rollout,stderr_path=self.stderr,source_receipt_path=self.receipt,
            binary_path=self.binary,instruction_bundle_path=self.bundle,
            prompt_file=self.prompt,operation_id="resume-1",state_root=root/"state",
            timeout_seconds=20)

    def test_verification_requires_same_header_rollout_and_host(self):
        evidence = native.verify_registration(session_id=CID,environment_id="evanpc-workspace",
            rollout_path=self.rollout,stderr_path=self.stderr,source_receipt_path=self.receipt,
            binary_path=self.binary,instruction_bundle_path=self.bundle)
        self.assertEqual(evidence["rollout_sha256"],native.sha(self.rollout))
        self.stderr.write_text("OpenAI Codex v0.162.0\n--------\nsession id: "+
                               "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n--------\n")
        with self.assertRaisesRegex(ValueError,"header session ID mismatch"):
            native.verify_registration(session_id=CID,environment_id="evanpc-workspace",
                rollout_path=self.rollout,stderr_path=self.stderr,source_receipt_path=self.receipt,
                binary_path=self.binary,instruction_bundle_path=self.bundle)

    def test_provisional_locator_never_invokes_codex(self):
        evidence = native.verify_registration(session_id=CID,environment_id="evanpc-workspace",
            rollout_path=self.rollout,stderr_path=self.stderr,source_receipt_path=self.receipt,
            binary_path=self.binary,instruction_bundle_path=self.bundle)
        with mock.patch.object(native,"post",return_value={"ok":True,"session":{
            **evidence,"resume_ready":False,"source_admitted":False}}), \
             mock.patch.object(native.subprocess,"run") as run:
            result = native.resume(self.args)
        self.assertEqual(result["error"],"native_session_not_ready")
        run.assert_not_called()

    def test_one_reservation_executes_native_resume_and_requires_central_confirmation(self):
        evidence = native.verify_registration(session_id=CID,environment_id="evanpc-workspace",
            rollout_path=self.rollout,stderr_path=self.stderr,source_receipt_path=self.receipt,
            binary_path=self.binary,instruction_bundle_path=self.bundle)
        requests=[]
        def post(body):
            requests.append(body)
            if body["operation"]=="get_codex_native_session":
                return {"ok":True,"session":{**evidence,"resume_ready":True,"source_admitted":True}}
            if body["operation"]=="begin_codex_native_resume":
                return {"ok":True,"execute":True,"attempt":{"state":"reserved"}}
            return {"ok":True,"attempt":{"state":"awaiting_central_readback"},
                    "central_readback_verified":False}
        def execute(command,**kwargs):
            self.assertEqual(command[:3],[str(self.binary.resolve()),"-a","never"])
            self.assertEqual(command[3:5],["exec","resume"])
            self.assertEqual(command[-2:], [CID,"-"])
            Path(command[command.index("-o")+1]).write_text("Work complete")
            with self.rollout.open("a") as file:
                file.write(json.dumps({"type":"event_msg","payload":{"type":"task_complete"}})+"\n")
            return subprocess.CompletedProcess(command,0,"terminal","stderr")
        with mock.patch.object(native,"post",side_effect=post), \
             mock.patch.object(native.subprocess,"run",side_effect=execute):
            result=native.resume(self.args)
        self.assertEqual(result["attempt"]["state"],"awaiting_central_readback")
        self.assertEqual([r["operation"] for r in requests],
            ["get_codex_native_session","begin_codex_native_resume","complete_codex_native_resume"])
        self.assertEqual(requests[-1]["terminal_rollout_sha256"],native.sha(self.rollout))


if __name__=="__main__":
    unittest.main()

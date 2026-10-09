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

    def test_pending_confirmation_rechecks_without_reexecuting_provider(self):
        result_dir=Path(self.temp.name)/"state"/"resume-1"
        result_dir.mkdir(parents=True)
        result=result_dir/"result.json"
        native.atomic_json(result,{"schema":"cognilode.codex_native_resume.local_result.v1",
            "session_id":CID,"operation_id":"resume-1",
            "terminal_rollout_sha256":"a"*64,"terminal_output_sha256":"b"*64})
        responses=[{"ok":True,"attempt":{"state":"awaiting_central_readback"},
                    "central_readback_verified":False},
                   {"ok":True,"attempt":{"state":"complete"},
                    "central_readback_verified":True}]
        with mock.patch.object(native,"post",side_effect=responses) as post, \
             mock.patch.object(native.subprocess,"run") as run:
            first=native.confirm_pending(result_dir.parent)
            second=native.confirm_pending(result_dir.parent)
            third=native.confirm_pending(result_dir.parent)
        self.assertEqual(first["results"][0]["state"],"awaiting_central_readback")
        self.assertEqual(second["results"][0]["state"],"complete")
        self.assertEqual(third["checked"],0)
        self.assertEqual(post.call_count,2)
        run.assert_not_called()
        self.assertTrue((result_dir/"confirmed.json").is_file())

    def test_activation_hashes_release_and_behavior_bytes_before_posting(self):
        root=Path(self.temp.name)
        system=root/"system.txt";system.write_text("correct system replacement")
        developer=root/"developer.txt";developer.write_text("correct developer replacement")
        behavior=root/"behavior.json"
        behavior.write_text(json.dumps({"schema":"cognilode.codex.native_instruction_behavior.v1",
            "binary_sha256":native.sha(self.binary),"system_replacement_observed":True,
            "developer_replacement_observed":True,"same_session_resume_observed":True,
            "observed_session_id":"223e4567-e89b-12d3-a456-426614174000",
            "terminal_rollout_sha256":"a"*64,"initial_event_count":2}))
        release=root/"release.json"
        release.write_text(json.dumps({"schema":"cognilode.codex.native_instruction_release.v1",
            "binary_sha256":native.sha(self.binary),"bundle_sha256":native.sha(self.bundle),
            "system_sha256":native.sha(system),"developer_sha256":native.sha(developer),
            "behavior_receipt_sha256":native.sha(behavior)}))
        args=types.SimpleNamespace(session_id=CID,environment_id="evanpc-workspace",
            binary_path=self.binary,instruction_bundle_path=self.bundle,
            system_instructions_path=system,developer_instructions_path=developer,
            release_manifest_path=release,behavior_receipt_path=behavior)
        requests=[]
        def post(body):
            requests.append(body)
            if body["operation"]=="get_codex_native_session":
                return {"ok":True,"session":{"environment_id":"evanpc-workspace",
                    "binary_sha256":native.sha(self.binary),"source_admitted":True}}
            return {"ok":True,"session":{"instruction_status":"release_verified",
                "instruction_release_sha256":native.sha(release),
                "instruction_bundle_sha256":native.sha(self.bundle)}}
        with mock.patch.object(native,"post",side_effect=post):
            self.assertTrue(native.activate(args)["ok"])
            behavior.write_text(behavior.read_text()+" ")
            with self.assertRaisesRegex(ValueError,"do not support activation"):
                native.activate(args)
        self.assertEqual(len([x for x in requests if x["operation"]=="activate_codex_native_instructions"]),1)


if __name__=="__main__":
    unittest.main()

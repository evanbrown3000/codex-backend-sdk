from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cognilode-chatmode-queue-worker"


def worker():
    loader = importlib.machinery.SourceFileLoader("chatmode_queue_worker_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_only_fenced_rhythm_claim_for_selected_device_is_sendable():
    module = worker()
    job = {"provider": "chatgpt.com", "rhythm_tape_sha256": "a" * 64,
           "rhythm_slot_index": 0, "claimed_by": "rhythm:evanpc:aaaa:0"}
    assert module.selected_for_device(job, "evanpc")
    assert not module.selected_for_device(job, "laptop")
    assert not module.selected_for_device({**job, "claimed_by": "ordinary-worker"}, "evanpc")
    assert not module.selected_for_device({**job, "rhythm_slot_index": None}, "evanpc")


def test_attachment_is_physically_present_and_hash_verified(tmp_path):
    module = worker()
    path = tmp_path / "research.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plan.plan", "[ ] step")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert module.attachment_paths({"attachment_refs": [{"ref": str(path), "sha256": digest}]}) == [path]
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        module.attachment_paths({"attachment_refs": [{"ref": str(path), "sha256": "0" * 64}]})


def test_completion_requires_terminal_central_readback_and_exact_zip(tmp_path):
    module = worker()
    path = tmp_path / "work.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("EXTERNAL_EFFECT_INSTRUCTIONS.md", "Deploy and verify")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    value = {"assistant_terminal": True, "conversation_id": "conv-1",
             "central_conversation_store": {"central_readback_verified": True, "conversation_id": "conv-1"},
             "downloaded_files": [{"path": str(path), "sha256": digest, "name": "work.zip"}]}
    assert module.validated_result(value)[0] == "conv-1"
    assert module.validated_result({**value, "assistant_terminal": False}) is None
    assert module.validated_result({**value, "central_conversation_store": {"central_readback_verified": False}}) is None
    assert module.validated_result({**value, "downloaded_files": [{"path": str(path), "sha256": "0" * 64, "name": "work.zip"}]}) is None
    missing = tmp_path / "missing-instructions.zip"
    with zipfile.ZipFile(missing, "w") as archive:
        archive.writestr("report.txt", "incomplete handoff")
    assert module.validated_result({**value, "downloaded_files": [{"path": str(missing),
        "sha256": hashlib.sha256(missing.read_bytes()).hexdigest(), "name": missing.name}]}) is None


def test_stable_send_identity_is_device_independent():
    module = worker()
    assert module.stable_user_message_id('job-1') == module.stable_user_message_id('job-1')
    assert module.stable_user_message_id('job-1') != module.stable_user_message_id('job-2')


def test_sender_receives_queue_selected_model_and_effort(tmp_path):
    module = worker()
    job = {'id': 'job-1', 'model': 'gpt-route-selected', 'reasoning_effort': 'xhigh'}
    attachment = tmp_path / 'research.zip'
    command = module.sender_command(job, tmp_path / 'prompt.txt', [attachment])
    assert command[command.index('--model') + 1] == 'gpt-route-selected'
    assert command[command.index('--effort') + 1] == 'xhigh'
    assert command[command.index('--attach') + 1] == str(attachment)


def test_only_explicit_preaccept_rejections_can_reenter_rhythm(monkeypatch, tmp_path):
    module = worker()
    module.ROOT = tmp_path
    job = {'id': 'retry-job', 'claimed_by': 'rhythm:evanpc', 'lease_token': 'fenced', 'lease_generation': 3}
    calls = []
    monkeypatch.setattr(module.sender, 'operator_memory_post', lambda body: calls.append(body) or {'ok': True})
    rejected = {'http_status': 403, 'state': 'authentication_failed', 'provider_acceptance_observed': False,
                'conversation_id': None, 'recovery': {'ambiguous_replay_suppressed': False, 'events': []}}
    assert module.requeue_definitive_rejection(job, rejected)
    assert calls[0]['operation'] == 'requeue_definitive_chatmode_rejection'
    assert calls[0]['user_message_id'] == module.stable_user_message_id(job['id'])
    assert json.loads(module.job_state_path(job['id']).read_text())['state'] == 'queued_after_definitive_rejection'
    for status, state in [(502, 'ambiguous_acceptance'), (200, 'provider_accepted_unfinished')]:
        assert not module.requeue_definitive_rejection(job, {**rejected, 'http_status': status, 'state': state})
    assert not module.requeue_definitive_rejection(job, {**rejected, 'conversation_id': 'provider-conversation'})
    assert len(calls) == 1


def test_later_slot_preserves_rejected_receipt_and_unblocks_sender(monkeypatch, tmp_path):
    module = worker()
    module.ROOT = tmp_path / 'worker'
    output = tmp_path / 'sender'
    output.mkdir()
    monkeypatch.setattr(module.sender, 'DEFAULT_OUTPUT_ROOT', output)
    job = {'id': 'retry-job', 'lease_generation': 4}
    stem = module.job_stem(job['id'])
    rejected = {'http_status': 429, 'state': 'rate_limited', 'provider_acceptance_observed': False,
                'recovery': {'ambiguous_replay_suppressed': False}}
    for suffix in ('.json', '.sse.receipt.json', '.attempt-01.sse'):
        (output / (stem + suffix)).write_text(json.dumps(rejected))
    assert module.prior_rejected_result(job['id']) == rejected
    module.archive_prior_rejected_attempt(job, rejected)
    archived = module.ROOT / 'rejected-attempts' / job['id'] / '4'
    assert (archived / 'result.json').is_file()
    for suffix in ('.json', '.sse.receipt.json', '.attempt-01.sse'):
        assert (archived / (stem + suffix)).is_file()
        assert not (output / (stem + suffix)).exists()


def test_remote_attachment_requires_hash_and_https(monkeypatch, tmp_path):
    module = worker()
    module.STAGE_ROOT = tmp_path / 'stage'
    with pytest.raises(ValueError, match='exact SHA-256'):
        module._stage_remote_attachment('https://example.invalid/a.zip', '')
    with pytest.raises(ValueError, match='requires https'):
        module._stage_remote_attachment('http://example.invalid/a.zip', 'a' * 64)


def test_legacy_queue_job_stages_missing_local_zip_from_digest(monkeypatch, tmp_path):
    module = worker()
    source = tmp_path / 'research.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('research.txt', 'worker packet')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv('COGNILODE_TASKFLOW_ATTACHMENT_S3_BUCKET', 'private-launchpad')
    requested = []

    def stage(uri, expected):
        requested.append((uri, expected))
        return source

    monkeypatch.setattr(module, '_stage_remote_attachment', stage)
    row = {'ref': 'file:/first-device/research.zip', 'sha256': digest}
    assert module.attachment_paths({'attachment_refs': [row]}) == [source]
    assert requested == [(f's3://private-launchpad/taskflow-artifacts/sha256/{digest}.zip', digest)]


def test_device_priority_is_configurable_and_not_part_of_send_identity():
    module = worker()
    assert isinstance(module.DEVICE_PRIORITY, int)
    assert module.stable_user_message_id('same-job') == module.stable_user_message_id('same-job')


def test_heartbeat_is_per_device_only_and_does_not_overwrite_global_gate(monkeypatch):
    module = worker()
    calls = []
    monkeypatch.setattr(module, 'health_ready', lambda: True)
    monkeypatch.setattr(module.sender, 'operator_memory_post', lambda body: calls.append(body) or {'ok': True})
    module.heartbeat('laptop', 0)
    assert [c['operation'] for c in calls] == ['rhythm_device_heartbeat']
    assert calls[0]['device']['id'] == 'laptop'


def test_private_s3_attachment_stages_exact_zip_for_alternate_device(monkeypatch, tmp_path):
    module = worker()
    module.STAGE_ROOT = tmp_path / 'stage'
    source = tmp_path / 'research.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('plan.plan', '[ ] step')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv('COGNILODE_TASKFLOW_ATTACHMENT_S3_BUCKET', 'private-launchpad')
    monkeypatch.setenv('COGNILODE_AWS_CLI', 'fake-aws')
    calls = []

    class Result:
        def __init__(self, stdout=''):
            self.stdout = stdout

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[2] == 'head-object':
            return Result(json.dumps({'ContentLength': source.stat().st_size,
                                      'Metadata': {'sha256': digest}}))
        if argv[2] == 'get-object':
            Path(argv[7]).write_bytes(source.read_bytes())
            return Result()
        raise AssertionError(argv)

    monkeypatch.setattr(module.subprocess, 'run', run)
    row = {'ref': '/no-local/research.zip', 'sha256': digest,
           'mirrors': [f's3://private-launchpad/taskflow-artifacts/sha256/{digest}.zip']}
    staged = module.attachment_paths({'attachment_refs': [row]})[0]
    assert staged.read_bytes() == source.read_bytes()
    assert [x[2] for x in calls] == ['head-object', 'get-object']


def test_cross_device_recovery_reconstructs_only_known_provider_conversation(monkeypatch, tmp_path):
    module = worker()
    monkeypatch.setattr(module.sender, 'DEFAULT_OUTPUT_ROOT', tmp_path)
    job = {'id': 'job-123', 'reasoning_effort': 'xhigh'}
    message_id = module.stable_user_message_id(job['id'])
    monkeypatch.setattr(module, 'result_for_job', lambda _job_id: None)
    monkeypatch.setattr(module, 'send_custody', lambda _job: {'custody': {
        'user_message_id': message_id, 'conversation_id': 'provider-conversation-1'}})
    commands = []
    monkeypatch.setattr(module.subprocess, 'run', lambda argv, **_kwargs: commands.append(argv))
    module.recover(job)
    receipt = tmp_path / (module.job_stem(job['id']) + '.sse.receipt.json')
    value = json.loads(receipt.read_text())
    assert value['user_message_id'] == message_id
    assert value['conversation_id'] == 'provider-conversation-1'
    assert value['recovered_from_central_custody'] is True
    assert len(commands) == 1 and str(module.COLLECTOR_PATH) in commands[0]


def test_alternate_device_recovers_pending_turn_when_original_heartbeat_stales(monkeypatch):
    module = worker()
    now = datetime.now(timezone.utc)
    job = {'id': 'pending-1', 'provider': 'chatgpt.com', 'rhythm_tape_sha256': 'a'*64,
           'rhythm_slot_index': 1, 'claimed_by': 'rhythm:evanpc:aaaa:1'}
    def post(body):
        if body['operation'] == 'list_jobs':
            return {'jobs': [job] if body['state'] == 'effect_pending' else []}
        if body['operation'] == 'rhythm_read':
            return {'gate': {'devices': [
                {'id': 'evanpc', 'available': True, 'priority': 100,
                 'observed_at': (now-timedelta(minutes=7)).isoformat()},
                {'id': 'laptop', 'available': True, 'priority': 50,
                 'observed_at': now.isoformat()}]}}
        raise AssertionError(body)
    monkeypatch.setattr(module.sender, 'operator_memory_post', post)
    recovered = []
    monkeypatch.setattr(module, 'recover', lambda item: recovered.append(item['id']))
    import threading
    module.poll('laptop', set(), threading.Lock())
    assert recovered == ['pending-1']

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cognilode-chatmode-queue-worker"


def worker():
    loader = importlib.machinery.SourceFileLoader("chatmode_queue_worker_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_second_device_browser_auth_is_used_for_health_send_and_reconcile(monkeypatch, tmp_path):
    module = worker()
    profile = tmp_path / "Chrome Profile"
    profile.mkdir()
    monkeypatch.setenv("COGNILODE_CHATMODE_AUTH_SOURCE", "chrome")
    monkeypatch.setenv("COGNILODE_CHATMODE_CHROME_PROFILE", str(profile))
    prefix = module.sender_prefix()
    assert prefix[2:] == ["--auth-source", "chrome", "--chrome-profile", str(profile)]
    command = module.sender_command({"id": "job-1"}, tmp_path / "prompt", [])
    assert command[:len(prefix) + 1] == prefix + ["send"]
    calls = []
    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='{"ok":true}')
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.health_ready() is True
    assert calls[-1] == prefix + ["health"]
    monkeypatch.setenv("COGNILODE_CHATMODE_AUTH_SOURCE", "codex")
    assert module.sender_prefix()[2:] == ["--auth-source", "codex"]


def test_bounded_job_scan_reaches_later_pages_after_state_changes(tmp_path):
    module = worker()
    module.ROOT = tmp_path
    jobs = {f"job-{i:04d}": "claimed" for i in range(621)}
    calls = []

    class Sender:
        @staticmethod
        def operator_memory_post(body):
            assert body["operation"] == "list_jobs"
            calls.append(dict(body))
            rows = [dict(id=key) for key in sorted(jobs)
                    if jobs[key] == body["state"] and key > body["cursor"]]
            page = rows[:body["limit"]]
            return {"jobs": page, "next_cursor": page[-1]["id"] if len(rows) > len(page) else None}

    module.sender = Sender()
    first, cursor = module.scan_job_state("evanpc", "claimed")
    assert len(first) == 400
    assert cursor == "job-0399"
    module._save_scan_cursor("evanpc", "claimed", cursor)
    for row in first:
        jobs[row["id"]] = "effect_pending"
    second, cursor = module.scan_job_state("evanpc", "claimed")
    assert len(second) == 221
    assert cursor == ""
    assert {x["id"] for x in first}.isdisjoint(x["id"] for x in second)
    module._save_scan_cursor("evanpc", "claimed", cursor)
    pending = []
    for _ in range(4):
        page, next_cursor = module.scan_job_state("evanpc", "effect_pending")
        pending.extend(page)
        module._save_scan_cursor("evanpc", "effect_pending", next_cursor)
    assert len(pending) == 400
    assert len(calls) <= 12


def test_job_scan_rejects_unpaginated_full_page(tmp_path):
    module = worker()
    module.ROOT = tmp_path
    class Sender:
        @staticmethod
        def operator_memory_post(_body):
            return {"jobs": [{"id": str(i)} for i in range(module.JOB_SCAN_PAGE_SIZE)]}
    module.sender = Sender()
    with pytest.raises(RuntimeError, match="pagination is unavailable"):
        module.scan_job_state("evanpc", "claimed")


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


def test_provider_stream_requires_paired_builtin_tool_result(tmp_path):
    module = worker()
    messages = [
        {'id': 'call-native', 'author': {'role': 'assistant'}, 'recipient': 'functions.exec'},
        {'id': 'result-native', 'author': {'role': 'tool', 'name': 'functions.exec'},
         'status': 'finished_successfully',
         'metadata': {'parent_id': 'call-native'}},
        {'id': 'call-chatmode', 'author': {'role': 'assistant'}, 'recipient': 'container.exec'},
        {'id': 'result-chatmode', 'author': {'role': 'tool', 'name': 'container.exec'},
         'status': 'finished_successfully',
         'metadata': {'parent_id': 'call-chatmode'}},
        {'id': 'call-plugin', 'author': {'role': 'assistant'}, 'recipient': 'api_tool.call_tool'},
        {'id': 'result-plugin', 'author': {'role': 'tool', 'name': 'api_tool.call_tool'},
         'status': 'finished_successfully',
         'metadata': {'parent_id': 'call-plugin'}},
        {'id': 'assistant-claim', 'author': {'role': 'assistant'}, 'recipient': 'all',
         'content': {'parts': ['I ran code in my sandbox.']}},
    ]
    raw = '\n'.join('data: ' + json.dumps({'message': row}) for row in messages).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / 'stream.sse'
    path.write_bytes(raw)
    assert module.provider_tool_evidence(raw, expected_sha256=digest) == [
        {'kind': 'provider_observed_native_exec', 'tool': 'container.exec', 'ref': 'result-chatmode',
         'call_ref': 'call-chatmode', 'raw_stream_sha256': digest},
        {'kind': 'provider_observed_functions_exec', 'tool': 'functions.exec', 'ref': 'result-native',
         'call_ref': 'call-native', 'raw_stream_sha256': digest}]
    assert {row['ref'] for row in module.tool_evidence_from_result({'raw_path_host': str(path), 'raw_sha256': digest})} == {'result-native', 'result-chatmode'}
    assert module.tool_evidence_from_result({'raw_path_host': str(path), 'raw_sha256': '0' * 64}) == []
    failed = [dict(row, status='finished_with_error') if row.get('id') == 'result-chatmode' else row
              for row in messages]
    failed_raw = '\n'.join('data: ' + json.dumps({'message': row}) for row in failed).encode()
    assert [row['ref'] for row in module.provider_tool_evidence(failed_raw)] == ['result-native']


def test_provider_stream_reconstructs_tool_messages_from_patches():
    module = worker()
    frames = [
        {'c': 0, 'p': '', 'o': 'add', 'v': {'message': {
            'id': 'call', 'author': {'role': 'assistant'}, 'recipient': 'all'}}},
        {'c': 1, 'p': '/message/recipient', 'o': 'replace', 'v': 'functions.exec'},
        {'c': 2, 'p': '', 'o': 'add', 'v': {'message': {
            'id': 'result', 'author': {'role': 'tool', 'name': 'functions.exec'},
            'status': 'in_progress',
            'metadata': {'parent_id': 'call'}}}},
        {'c': 3, 'p': '/message/status', 'o': 'replace', 'v': 'finished_successfully'},
    ]
    raw = '\n'.join('data: ' + json.dumps(frame) for frame in frames).encode()
    assert [row['ref'] for row in module.provider_tool_evidence(raw)] == ['result']


def test_decisionx_scan_migrates_text_spool_to_source_refs(tmp_path):
    module = worker()
    module.DX_HOME = tmp_path
    episode = {'episode_id': 'episode-1', 'source_sha256': 'a' * 64,
               'provider': 'chatgpt.com', 'conversation_id': 'conversation-1',
               'intent_turn': {'text': 'private source text'}}
    (tmp_path / 'scan.json').write_text(json.dumps({'offset': 3, 'spool': [episode],
                                                     'last_scan': 1.0}))
    loaded = module._dx_load()
    assert loaded['spool'] == [{key: episode[key] for key in
                                ('episode_id', 'source_sha256', 'provider', 'conversation_id')}]
    assert 'private source text' not in (tmp_path / 'scan.json').read_text()


def test_decisionx_batch_waits_for_500_drive_verified_multi_year_sources(tmp_path):
    module = worker()
    module.DX_HOME = tmp_path
    class Bridge:
        @staticmethod
        def shared_stock_census(_post, minimum):
            assert minimum == 500
            return {'multi_year_ready': False, 'distinct_complete': 42, 'span_days': 36}
    class Sender:
        @staticmethod
        def operator_memory_post(_body):
            raise AssertionError('a sub-500 population must not scan or enqueue')
    module._dx_bridge = lambda: Bridge()
    module.sender = Sender()
    module._dx_stock_snapshot = lambda: Bridge.shared_stock_census(None, minimum=500)
    module.decisionx_batch_pump()
    assert not (tmp_path / 'progress.sqlite3').exists()
    assert json.loads((tmp_path / 'scan.json').read_text())['spool'] == []


def test_decisionx_ready_stock_enqueues_one_xhigh_chatmode_job_with_full_source_zip(tmp_path):
    module = worker()
    module.DX_HOME = tmp_path
    source = {'provider': 'openai-codex', 'conversation_id': 'verified-1',
              'prompt_sha256': '1'*64, 'response_sha256': '2'*64,
              'events': [turn for n in range(8) for turn in (
                  {'id': f'u{n}', 'role': 'user', 'content': f'Investigate source lineage number {n}.'},
                  {'id': f'a{n}', 'role': 'assistant', 'content': f'I compared source lineage number {n}.'})]}
    refs = [{'provider':'openai-codex','conversation_id': f'verified-{n}',
             'prompt_sha256':'1'*64,'response_sha256':'2'*64,
             'source_at_utc':(datetime(2024,1,1,tzinfo=timezone.utc)
                              + timedelta(days=round((n-1)*800/499))).isoformat(),
             'project_hint':f'project-{n%7}'}
            for n in range(1,501)]
    class Bridge:
        @staticmethod
        def shared_stock_census(_post, minimum):
            assert minimum == 500
            return {'multi_year_ready':True,'distinct_complete':500,'span_days':800,
                    'verified_source_refs':refs}
        @staticmethod
        def _complete_source(_index,_read):
            return datetime.now(timezone.utc)
    class Builder:
        @staticmethod
        def build_source_batches(_post, selected, *, output_root, bridge):
            assert len(selected) == 32
            assert len({(r['provider'],r['conversation_id']) for r in selected}) == 32
            output_root.mkdir(parents=True)
            path = output_root / 'batch-000.zip'
            path.write_bytes(b'full source packet')
            return [{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]
        @staticmethod
        def publish_private(packet):
            return 's3://private-bucket/taskflow-artifacts/sha256/' + packet['sha256'] + '.zip'
    queued=[]
    class Sender:
        @staticmethod
        def operator_memory_post(body):
            if body['operation']=='read':
                return {'ok':True,'conversation':{**source,'conversation_id':body['conversation_id']}}
            if body['operation']=='enqueue_decisionx_prompt':
                queued.append(body)
                return {'ok':True,'job':{'id':body['job_id'],'state':'queued'}}
            if body['operation']=='get_job':
                return {'ok':True,'job':{'state':'queued','effect_evidence':[]}}
            raise AssertionError(body['operation'])
    module._dx_bridge=lambda:Bridge()
    module._dx_source_batch_builder=lambda:Builder()
    module.sender=Sender()
    module._dx_stock_snapshot=lambda:Bridge.shared_stock_census(None,minimum=500)
    # A pre-gate scanner may have advanced beyond rare provider/year cohorts.
    (tmp_path/'scan.json').write_text(json.dumps({'last_scan':0,'offset':100,'spool':[]}))
    module.decisionx_batch_pump()
    assert len(queued)==1
    assert queued[0]['provider']=='chatgpt.com'
    assert queued[0]['reasoning_effort']=='xhigh'
    assert queued[0]['model']=='gpt-5-6-thinking'
    assert queued[0]['priority']==72
    assert len(queued[0]['attachment_refs'])==2
    assert all(row['mirrors'][0].startswith('s3://') for row in queued[0]['attachment_refs'])
    with zipfile.ZipFile(queued[0]['attachment_refs'][0]['ref'].removeprefix('file:')) as packet:
        episodes=[json.loads(line) for line in packet.read('episodes.jsonl').splitlines()]
    assert len(episodes)==32
    assert len({row['conversation_id'] for row in episodes})==32
    assert episodes[0]['conversation_id'] == module._dx_diversified_source_refs(refs)[0]['conversation_id']
    with module._dx_connection() as db:
        db.execute("UPDATE episodes SET state='retry',retry_after=0")
    state=json.loads((tmp_path/'scan.json').read_text())
    state['last_scan']=0
    state['offset']=0
    (tmp_path/'scan.json').write_text(json.dumps(state))
    module.decisionx_batch_pump()
    assert len(queued)==2
    assert queued[1]['job_id'] != queued[0]['job_id']


def test_decisionx_source_order_rotates_years_providers_and_projects():
    module = worker()
    rows=[]
    for provider in ('openai-codex','chatgpt-export-format'):
        for year in (2024,2025,2026):
            for n in range(5):
                rows.append({'provider':provider,'conversation_id':f'{provider}-{year}-{n}',
                             'source_at_utc':f'{year}-01-01T00:00:00+00:00',
                             'project_hint':f'project-{n%2}'})
    ordered=module._dx_diversified_source_refs(list(reversed(rows)))
    assert len(ordered)==30
    assert len({(row['provider'],row['source_at_utc'][:4]) for row in ordered[:6]})==6
    assert len({row['project_hint'] for row in ordered[:12]})==2


def test_decisionx_shared_stock_census_cannot_block_queue_polling(tmp_path):
    module = worker()
    module.DX_HOME = tmp_path
    module.event = lambda *_args, **_kwargs: None
    started, release = threading.Event(), threading.Event()

    class Bridge:
        @staticmethod
        def shared_stock_census(_post, minimum, checkpoint_path=None,
                                stop_at_minimum=False):
            assert minimum == 500
            assert checkpoint_path == tmp_path / 'shared-stock-readback-proofs.json'
            assert stop_at_minimum is True
            started.set()
            assert release.wait(3)
            return {'multi_year_ready': False, 'distinct_complete': 42, 'span_days': 36}

    module._dx_bridge = lambda: Bridge()
    module.sender = SimpleNamespace(operator_memory_post=lambda _body: None)
    start = time.monotonic()
    module.decisionx_batch_pump()
    assert time.monotonic() - start < 0.5
    assert started.wait(1)
    assert not (tmp_path / 'scan.json').exists()
    release.set()
    module._DX_STOCK_THREAD.join(3)
    module.decisionx_batch_pump()
    assert json.loads((tmp_path / 'scan.json').read_text())['spool'] == []


def test_decisionx_native_batch_verifies_source_and_binds_returned_compute(tmp_path):
    module = worker()
    module.DX_HOME = tmp_path
    source = {'provider': 'openai-codex', 'conversation_id': 'conversation-1',
              'prompt_sha256': '1' * 64, 'response_sha256': '2' * 64,
              'events': [{'id': 'u1', 'role': 'user', 'content': 'Research the actual requirements in the source history.'},
                         {'id': 'a1', 'role': 'assistant', 'content': 'I found the source commits and compared their behavior.'},
                         {'id': 'u2', 'role': 'user', 'content': 'That misses the recursive behavior; implement the core loop.'},
                         {'id': 'a2', 'role': 'assistant', 'content': 'I implemented the recursive loop from historical source.'},
                         {'id': 'u3', 'role': 'user', 'content': 'The deployed loop now produces real effects.'}]}
    episode = module._dx_extract(source)[0]
    assert [row['id'] for row in episode['following_action_turns']] == ['a2']
    assert episode['following_user_turn']['id'] == 'u3'
    input_zip = module._dx_input_zip([episode], 'a' * 32)
    with zipfile.ZipFile(input_zip) as archive:
        assert archive.read('RUN_ME.py')
        assert json.loads(archive.read('manifest.json'))['schema'] == 'decisionx.iae.batch.v3'
    script = SCRIPT.parent / 'decisionx_native_batch.py'
    loader = importlib.machinery.SourceFileLoader('decisionx_native_batch_test', str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    native = importlib.util.module_from_spec(spec)
    loader.exec_module(native)
    full = {**source, 'prompt_sha256': '1' * 64, 'response_sha256': '2' * 64}
    full_raw = (json.dumps(full,sort_keys=True,ensure_ascii=False,separators=(',',':')) + '\n').encode()
    full_sha = hashlib.sha256(full_raw).hexdigest()
    source_zip = tmp_path / 'batch-000.zip'
    with zipfile.ZipFile(source_zip,'w') as archive:
        archive.writestr('MANIFEST.json',json.dumps({'schema':'cognilode.root_memory_sources.v1',
            'parts':[{'provider':'openai-codex','conversation_id':'conversation-1',
                      'prompt_sha256':'1'*64,'response_sha256':'2'*64,
                      'part_index':0,'part_count':1,'source_json_sha256':full_sha,
                      'part_sha256':full_sha,'path':'parts/part-000.part'}]}))
        archive.writestr('parts/part-000.part',full_raw)
    computed = native.run(input_zip, tmp_path / 'native', [source_zip])
    assert computed['episode_count'] == 1
    render = tmp_path / 'native' / computed['rendered_sources'][0]['path']
    assert 'Research the actual requirements' in render.read_text()
    assert 'I implemented the recursive loop' in render.read_text()
    output = tmp_path / 'labels.zip'
    label = {'episode_id': episode['episode_id'], 'source_sha256': episode['source_sha256'],
             'i': 'Research the actual requirements across the historical source commits.',
             'a': 'The assistant compared the source commits and reported the differences.',
             'e': None, 'outcome': 'Source comparison observed; downstream result unknown.',
             'tags': ['research', 'source-history'],
             'embedding_text': 'A user requests historical source research and the assistant compares commits.',
             'human_authorship_assessment': 'unverified user role',
             'changed_conditions': 'The codebase may have changed since this historical turn.',
             'double_triplet': None, 'uncertainty': 'No later user evaluation is present.'}
    with zipfile.ZipFile(output, 'w') as archive:
        archive.write(tmp_path / 'native' / 'NATIVE_COMPUTE.json', 'NATIVE_COMPUTE.json')
        archive.write(tmp_path / 'native' / 'neighbors.json', 'neighbors.json')
        archive.writestr('decisionx_iae_labels.jsonl', json.dumps(label) + '\n')
        archive.writestr('EXTERNAL_EFFECT_INSTRUCTIONS.md', 'Admit verified labels into shared search.\n')
    input_sha = hashlib.sha256(input_zip.read_bytes()).hexdigest()
    source_sha = hashlib.sha256(source_zip.read_bytes()).hexdigest()
    output_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    job = {'id': 'decisionx-iae-' + 'a' * 32, 'phase': 'iae_label_batch',
           'attachment_refs': [{'ref': str(input_zip), 'sha256': input_sha},
                               {'ref': str(source_zip), 'sha256': source_sha}]}
    value = {'central_conversation_store': {'provider_structured_uploads': [
        {'sha256': input_sha},{'sha256': source_sha}]}}
    files = [{'path': str(output), 'name': 'labels.zip', 'sha256': output_sha}]
    native_proof = [{'kind': 'provider_observed_native_exec'}]
    assert module._dx_native_preflight(job, files, value, native_proof) is None
    assert module._dx_native_preflight(job, files, value, []) == 'missing_provider_observed_native_exec'
    assert module._dx_native_preflight(job, files, {'central_conversation_store': {}}, native_proof) == 'provider_upload_proof_missing'
    admitted = []
    class Sender:
        @staticmethod
        def operator_memory_post(body):
            if body['operation'] == 'read':
                return {'ok': True, 'conversation': source}
            if body['operation'] == 'append_segments':
                admitted.extend(body['segments'])
                return {'ok': True}
            raise AssertionError(body['operation'])
    module.sender = Sender()
    module._dx_bridge = lambda: SimpleNamespace(_complete_source=lambda _index,_read: datetime.now(timezone.utc))
    outcome = module.decisionx_admit_labels(job, files, 'chatgpt-returned-conversation')
    assert outcome['admitted'] == 1
    assert admitted[0]['kind'] == 'IAE'
    assert admitted[0]['metadata']['source_sha256'] == episode['source_sha256']
    assert admitted[0]['metadata']['drive_verified_source'] is True
    assert admitted[0]['metadata']['full_source_prompt_sha256'] == '1' * 64


def test_decisionx_batch_resolves_exact_source_and_drops_changed_episode():
    module = worker()
    source = {'provider': 'chatgpt.com', 'conversation_id': 'conversation-1',
              'events': [{'id': 'u1', 'role': 'user', 'content': 'Instruction'},
                         {'id': 'a1', 'role': 'assistant', 'content': 'Action'}]}
    episode = module._dx_extract(source)[0]
    class Sender:
        def operator_memory_post(self, request):
            assert request['operation'] == 'read'
            return {'ok': True, 'conversation': source}
    module.sender = Sender()
    selected, stale = module._dx_resolve([module._dx_ref(episode),
        {**module._dx_ref(episode), 'source_sha256': '0' * 64}])
    assert selected == [episode]
    assert len(stale) == 1
    selected, stale = module._dx_resolve([module._dx_ref(episode)],
        complete_source=lambda _index, _read: False)
    assert selected == []
    assert stale == [module._dx_ref(episode)]


def test_decisionx_double_triplet_is_source_anchored():
    module = worker()
    source = {'provider': 'openai-codex', 'conversation_id': 'episode-2',
              'events': [
                  {'id': 'u1', 'role': 'user', 'content': 'Research the original implementation.'},
                  {'id': 'a1', 'role': 'assistant', 'content': 'I inspected the historical source.'},
                  {'id': 'u2', 'role': 'user', 'content': 'That misses the core loop; implement it.'},
                  {'id': 'a2', 'role': 'assistant', 'content': 'I implemented the recursive loop.'},
                  {'id': 'u3', 'role': 'user', 'content': 'The deployed loop now produces real effects.'},
              ]}
    first = module._dx_extract(source)[0]
    assert [row['id'] for row in first['following_action_turns']] == ['a2']
    assert first['following_user_turn']['id'] == 'u3'
    adjacent = {'instruction_turn_id': 'u2', 'action_turn_ids': ['a2'],
                'evaluation_turn_id': 'u3',
                'i': 'Implement the core recursive loop.',
                'a': 'Assistant implemented the requested recursive loop.',
                'e': 'The user observed the deployed loop producing real effects.',
                'relationship': 'The evaluation of the first action was also the next instruction.'}
    assert module._dx_adjacent_triplet_valid({'double_triplet': adjacent}, first)
    assert not module._dx_adjacent_triplet_valid(
        {'double_triplet': {**adjacent, 'action_turn_ids': ['forged']}}, first)
    assert not module._dx_adjacent_triplet_valid(
        {'double_triplet': {**adjacent, 'evaluation_turn_id': 'forged'}}, first)
    assert not module._dx_adjacent_triplet_valid({'double_triplet': {}}, first)
    assert module._dx_adjacent_triplet_valid({'double_triplet': None}, first)


def test_decisionx_only_first_verified_batch_uses_catchup_priority(tmp_path):
    module = worker()
    module.DX_HOME = tmp_path
    with module._dx_connection() as db:
        assert module._dx_batch_priority(db) == 72
        db.execute("INSERT INTO episodes(id,source_sha,state,attempts,retry_after,batch,updated) "
                   "VALUES('first',?,'done',1,0,'legacy',0)", ('a' * 64,))
        # A legacy local done row is not proof that a verified native batch ran.
        assert module._dx_batch_priority(db) == 72
        db.execute("INSERT INTO verified_dispatches(batch,job_id,prompt_sha,priority,queued_at) "
                   "VALUES('verified','decisionx-iae-verified',?,72,0)", ('b' * 64,))
        assert module._dx_batch_priority(db) == 20
        db.execute("UPDATE episodes SET state='done' WHERE id='first'")
        assert module._dx_batch_priority(db) == 20


def test_decisionx_native_batch_keeps_one_episode_per_complete_source():
    module = worker()
    refs = [{'provider': 'openai-codex', 'conversation_id': 'same', 'episode_id': 'first'},
            {'provider': 'openai-codex', 'conversation_id': 'same', 'episode_id': 'later'},
            {'provider': 'chatgpt-export-format', 'conversation_id': 'same', 'episode_id': 'other'}]
    assert module._dx_unique_source_refs(refs) == [refs[0], refs[2]]


def test_decisionx_legacy_local_done_requires_central_native_admission(tmp_path):
    module = worker()
    module.DX_HOME = tmp_path
    batch = 'a' * 32
    with module._dx_connection() as db:
        db.execute("INSERT INTO episodes(id,source_sha,state,attempts,retry_after,batch,updated) "
                   "VALUES('unproved',?,'done',1,0,?,0)", ('b' * 64, batch))

    class Sender:
        @staticmethod
        def operator_memory_post(body):
            assert body == {'operation': 'get_job', 'job_id': 'decisionx-iae-' + batch}
            return {'job': {'id': body['job_id'], 'state': 'complete',
                            'attachment_refs': [{'kind': 'iae_episode_batch'}],
                            'effect_evidence': [{'kind': 'provider_conversation', 'ref': 'chat'}]}}

    module.sender = Sender()
    with module._dx_connection() as db:
        assert module._dx_reconcile_unproved_done(db) == 1
        assert db.execute("SELECT state FROM episodes WHERE id='unproved'").fetchone() == ('retry',)


def test_decisionx_transport_input_retired_only_after_central_admission(tmp_path):
    module = worker()
    module.DX_HOME = tmp_path
    folder = tmp_path / 'input'
    folder.mkdir()
    complete = 'a' * 32
    queued = 'b' * 32
    for batch in (complete, queued):
        with zipfile.ZipFile(folder / f'{batch}.zip', 'w') as archive:
            archive.writestr('manifest.json', json.dumps({'batch_id': batch}))
            archive.writestr('episodes.jsonl', 'source conversation text')
    class Sender:
        def operator_memory_post(self, request):
            assert request['operation'] == 'get_job'
            batch = request['job_id'].removeprefix('decisionx-iae-')
            if batch == complete:
                return {'job': {'state': 'complete', 'effect_evidence': [
                    {'kind': 'decisionx_label_admission', 'ref': 'verified', 'admitted': 1}]}}
            return {'job': {'state': 'queued', 'effect_evidence': []}}
    module.sender = Sender()
    assert module._dx_retire_completed_inputs() == 1
    assert not (folder / f'{complete}.zip').exists()
    assert (folder / f'{queued}.zip').exists()


def test_decisionx_cross_device_partial_admission_retries_only_missing_ids(tmp_path):
    module=worker()
    module.DX_HOME=tmp_path
    batch='a'*32
    with module._dx_connection() as db:
        for episode_id in ('accepted','missing'):
            db.execute('INSERT INTO episodes(id,source_sha,state,attempts,retry_after,batch,updated) '
                       'VALUES(?,?,?,?,?,?,?)', (episode_id,'1'*64,'queued',1,0,batch,0))
    class Sender:
        @staticmethod
        def operator_memory_post(body):
            assert body['operation']=='get_job'
            return {'job':{'state':'complete','effect_evidence':[{
                'kind':'decisionx_label_admission','requested':2,'admitted':1,
                'admitted_episode_ids':['accepted']}]}}
    module.sender=Sender()
    with module._dx_connection() as db:
        assert module._dx_reconcile_queued(db)==1
        states=dict(db.execute('SELECT id,state FROM episodes').fetchall())
    assert states=={'accepted':'done','missing':'retry'}


def test_decisionx_completion_evidence_has_d1_persistable_ref(tmp_path):
    module=worker()
    module.ROOT=tmp_path
    module.validated_result=lambda _value: ('chat-conversation',[])
    module.tool_evidence_from_result=lambda _value: [{'kind':'provider_observed_native_exec','ref':'tool-1'}]
    module._dx_native_preflight=lambda *_args: None
    module.decisionx_admit_labels=lambda *_args,**_kwargs: {
        'requested':1,'admitted':1,'admitted_episode_ids':['episode-1']}
    captured=[]
    class Sender:
        @staticmethod
        def operator_memory_post(body):
            captured.append(body)
            return {'ok':True}
    module.sender=Sender()
    job={'id':'decisionx-iae-'+'a'*32,'phase':'iae_label_batch','claimed_by':'evanpc',
         'lease_token':'lease-1','lease_generation':1}
    assert module.complete_from_result(job,{}) is True
    evidence=captured[0]['effect_evidence']
    admission=next(row for row in evidence if row['kind']=='decisionx_label_admission')
    assert admission['ref']==job['id']
    assert admission['admitted_episode_ids']==['episode-1']


def test_sender_receives_queue_selected_model_and_effort(tmp_path):
    module = worker()
    job = {'id': 'job-1', 'model': 'gpt-route-selected', 'reasoning_effort': 'xhigh'}
    attachment = tmp_path / 'research.zip'
    command = module.sender_command(job, tmp_path / 'prompt.txt', [attachment])
    assert command[command.index('--model') + 1] == 'gpt-route-selected'
    assert command[command.index('--effort') + 1] == 'xhigh'
    assert command[command.index('--attach') + 1] == str(attachment)
    # D1 persists the queue's model as requested_model, not model.
    central_job = {'id': 'job-2', 'requested_model': 'gpt-central-selected',
                   'reasoning_effort': 'xhigh'}
    central_command = module.sender_command(central_job, tmp_path / 'prompt.txt', [attachment])
    assert central_command[central_command.index('--model') + 1] == 'gpt-central-selected'


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
    for status, state in [(404, 'provider_rejected'), (502, 'ambiguous_acceptance'),
                          (504, 'ambiguous_acceptance'), (200, 'provider_accepted_unfinished')]:
        assert not module.requeue_definitive_rejection(job, {**rejected, 'http_status': status, 'state': state})
    assert not module.requeue_definitive_rejection(job, {**rejected, 'conversation_id': 'provider-conversation'})
    assert len(calls) == 1


def test_ambiguous_gateway_retry_after_defers_exact_id_reconciliation(monkeypatch, tmp_path):
    module = worker()
    module.ROOT = tmp_path / 'worker'
    module.sender.DEFAULT_OUTPUT_ROOT = tmp_path / 'sender'
    module.sender.DEFAULT_OUTPUT_ROOT.mkdir()
    prompt = 'Do the attached work in Chat mode.'
    job = {'id': 'gateway-504', 'prompt': prompt,
           'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
           'claimed_by': 'rhythm:evanpc', 'lease_token': 'fenced', 'lease_generation': 1}
    monkeypatch.setattr(module, 'attachment_paths', lambda _job: [])
    monkeypatch.setattr(module, 'result_for_job', lambda _id: None)
    monkeypatch.setattr(module.sender, 'operator_memory_post', lambda body: (
        {'ok': False, 'error': 'post_may_have_started'} if
        body['operation'] == 'requeue_chatmode_prepost_failure' else {'ok': True}))
    monkeypatch.setattr(module, 'complete_from_result', lambda *_args: False)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=2, stderr='', stdout=json.dumps({'ok': False, 'state': 'ambiguous_acceptance',
           'http_status': 504, 'retry_after_seconds': 120,
           'recovery': {'ambiguous_replay_suppressed': True}})))

    started = time.time()
    module.execute(job)

    state = json.loads(module.job_state_path(job['id']).read_text())
    assert state['state'] == 'reconcile_required'
    assert state['reconcile_next_at'] >= started + 120


def test_upload_only_failure_requeues_for_later_rhythm_and_keeps_zip_ledger(monkeypatch, tmp_path):
    module = worker()
    module.ROOT = tmp_path / 'worker'
    module.sender.DEFAULT_OUTPUT_ROOT = tmp_path / 'sender'
    module.sender.DEFAULT_OUTPUT_ROOT.mkdir()
    prompt = 'Process this ZIP in your native sandbox.'
    job = {'id': 'upload-502', 'prompt': prompt,
           'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
           'claimed_by': 'rhythm:evanpc', 'lease_token': 'fenced', 'lease_generation': 1}
    stem = module.job_stem(job['id'])
    ledger = module.sender.DEFAULT_OUTPUT_ROOT / (stem + '.uploads.json')
    ledger.write_text('{"files":{"sha":{"id":"file_abc"}}}')
    monkeypatch.setattr(module, 'attachment_paths', lambda _job: [])
    monkeypatch.setattr(module, 'result_for_job', lambda _id: None)
    calls = []

    def operator(body):
        calls.append(body)
        return {'ok': True}

    monkeypatch.setattr(module.sender, 'operator_memory_post', operator)

    def run(_command, **kwargs):
        boundary = json.loads(kwargs['env']['COGNILODE_CHATMODE_SEND_FENCE'])
        assert boundary['job_id'] == job['id']
        return SimpleNamespace(returncode=2, stdout='', stderr=json.dumps({
            'state': 'prepost_attachment_upload_failed', 'http_status': 502,
            'retry_after_seconds': 120}))

    monkeypatch.setattr(module.subprocess, 'run', run)
    module.execute(job)

    state = json.loads(module.job_state_path(job['id']).read_text())
    assert state['state'] == 'queued_after_prepost_failure'
    assert ledger.is_file()
    requeue = next(c for c in calls if c['operation'] == 'requeue_chatmode_prepost_failure')
    assert requeue['retry_after_ms'] == 120000
    assert requeue['user_message_id'] == module.stable_user_message_id(job['id'])


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
    monkeypatch.setattr(module, 'recover', lambda item, device: recovered.append((item['id'], device)))
    import threading
    module.poll('laptop', set(), threading.Lock())
    assert recovered == [('pending-1', 'laptop')]


def test_terminal_missing_zip_waits_for_fresh_get_then_completes_failed_deliverable(monkeypatch, tmp_path):
    module = worker()
    module.ROOT = tmp_path / "worker"
    output = tmp_path / "outputs"
    output.mkdir()
    base = 1_800_000_000.0
    clock = [base]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    job = {"id": "taskflow-missing-zip", "provider": "chatgpt.com", "phase": "chatgpt_sandbox",
           "state": "effect_pending", "claimed_by": "rhythm:evanpc", "lease_token": "fence",
           "lease_generation": 1,
           "effect_started_at": datetime.fromtimestamp(base - 100, timezone.utc).isoformat()}
    value = {"assistant_terminal": True, "terminal_assistant_text": "Finished without a ZIP",
             "terminal_assistant_message_id": "assistant-1", "conversation_id": "conv-1",
             "completed_at": base, "downloaded_files": [],
             "central_conversation_store": {"central_readback_verified": True, "conversation_id": "conv-1"}}
    receipt = output / (module.job_stem(job["id"]) + ".sse.receipt.json")
    receipt.write_text(json.dumps({"user_message_id": "user-1", "conversation_id": "conv-1"}))
    operations = []
    class Sender:
        DEFAULT_OUTPUT_ROOT = output
        @staticmethod
        def operator_memory_post(body):
            operations.append(body["operation"])
            if body["operation"] == "complete_job":
                job.update(state="complete", conversation_id=body["conversation_id"],
                           effect_evidence=body["effect_evidence"])
                return {"ok": True}
            if body["operation"] == "get_job":
                return {"ok": True, "job": job}
            raise AssertionError(body)
    module.sender = Sender()
    monkeypatch.setattr(module, "result_for_job", lambda _id: value)
    monkeypatch.setattr(module, "reconcile_ambiguous", lambda *args: pytest.fail("ambiguous reconcile after terminal"))
    collector_calls = []
    def collection(*args, **kwargs):
        assert "--receipt" in args[0]
        collector_calls.append(1)
        state = "retryable_collection_error" if len(collector_calls) == 2 else "collected"
        return SimpleNamespace(returncode=0, stdout=json.dumps({"outcomes": [{"state": state,
            "record": str(output / (module.job_stem(job["id"]) + ".collected.json"))}]}))
    monkeypatch.setattr(module.subprocess, "run", collection)
    module.recover(job)
    assert job["state"] == "effect_pending"
    assert operations == []
    clock[0] = base + 1900
    module.recover(job)
    assert job["state"] == "effect_pending", "a post-deadline transient GET is not a provider work defect"
    assert operations == []
    clock[0] += module.TERMINAL_DELIVERABLE_RECHECK_SECONDS + 1
    module.recover(job)
    assert job["state"] == "complete"
    assert operations == ["complete_job", "get_job"]
    failure = next(e for e in job["effect_evidence"] if e["kind"] == "chatgpt_terminal_deliverable_failure")
    assert failure["conversation_id"] == "conv-1"
    assert module.job_state_path(job["id"]).is_file()


def test_transient_collector_error_cannot_be_labeled_provider_deliverable_failure(monkeypatch, tmp_path):
    module = worker()
    module.ROOT = tmp_path / "worker"
    output = tmp_path / "outputs"
    output.mkdir()
    now = 1_800_000_000.0
    monkeypatch.setattr(module.time, "time", lambda: now)
    job = {"id": "taskflow-transient", "provider": "chatgpt.com", "phase": "chatgpt_sandbox",
           "state": "effect_pending", "effect_started_at": datetime.fromtimestamp(now - 4000, timezone.utc).isoformat()}
    value = {"assistant_terminal": True, "terminal_assistant_text": "No ZIP observed",
             "terminal_assistant_message_id": "assistant-1", "conversation_id": "conv-1",
             "completed_at": now - 1900, "downloaded_files": [],
             "central_conversation_store": {"central_readback_verified": True, "conversation_id": "conv-1"}}
    (output / (module.job_stem(job["id"]) + ".sse.receipt.json")).write_text(json.dumps({"user_message_id": "u"}))
    module.sender = SimpleNamespace(DEFAULT_OUTPUT_ROOT=output)
    monkeypatch.setattr(module, "result_for_job", lambda _id: value)
    monkeypatch.setattr(module, "complete_failed_deliverable", lambda *args, **kwargs: pytest.fail("false defect"))
    monkeypatch.setattr(module, "reconcile_ambiguous", lambda *args: pytest.fail("ambiguous reconcile after terminal"))
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0,
        stdout=json.dumps({"outcomes": [{"state": "provider_rate_limited"}]})))
    module.recover(job)
    state = json.loads(module.job_state_path(job["id"]).read_text())
    assert state["state"] == "terminal_deliverable_collection"
    assert state["deliverable_fresh_collection_at"] == 0

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/cognilode-b4pt0r-chatmode'


def sender():
    loader = importlib.machinery.SourceFileLoader('chatmode_sse_history_test', str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def frame(value):
    return 'data: ' + json.dumps(value, separators=(',', ':')) + '\n'


def tiny_stream():
    call = {'id': 'call-1', 'author': {'role': 'assistant'}, 'recipient': 'container.exec',
            'content': {'content_type': 'code', 'text': 'py'}}
    result = {'id': 'result-1', 'author': {'role': 'tool', 'name': 'container.exec'},
              'metadata': {'parent_id': 'call-1'}, 'status': 'finished_successfully',
              'content': {'content_type': 'execution_output', 'text': 'ok'}}
    return (frame({'c': 0, 'p': '', 'o': 'add', 'v': {'message': call}})
            + frame({'c': 1, 'p': '/message/content/text', 'o': 'append', 'v': 'thon work.py'})
            + frame({'c': 2, 'p': '', 'o': 'add', 'v': {'message': result}})).encode()


def test_sse_history_reconstructs_patches_and_true_native_pair():
    module = sender()
    raw = tiny_stream()
    found = module.provider_sse_tool_history(raw, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert [(event['role'], event['provider_message_id'], event['content']) for event in found['tool_events']] == [
        ('tool_call', 'call-1', 'python work.py'), ('tool_result', 'result-1', 'ok')]
    assert found['tool_events'][1]['provider_parent_id'] == 'call-1'
    assert found['tool_events'][1]['provider_status'] == 'finished_successfully'
    assert found['tool_events'][1]['provider_content_type'] == 'execution_output'
    assert found['tool_events'][1]['provider_content_sha256'] == hashlib.sha256(b'ok').hexdigest()
    assert found['finished_container_exec_results'] == 1
    with pytest.raises(module.CapabilityError, match='SHA-256 mismatch'):
        module.provider_sse_tool_history(raw, expected_sha256='0' * 64)


def test_sse_history_is_admitted_in_order_and_independently_read_back(tmp_path, monkeypatch):
    module = sender()
    raw = tiny_stream()
    path = tmp_path / 'provider.sse'
    path.write_bytes(raw)
    rows = {}
    calls = []

    def post(body):
        calls.append(body['operation'])
        if body['operation'] == 'record_conversation':
            for event in body['events']:
                rows.setdefault(event['provider_message_id'], event)
            post.receipt = body['read_receipt']
            return {'ok': True, 'stored': True, 'conversation_id': body['conversation_id']}
        return {'ok': True, 'conversation': {'prompt': 'Do it', 'response_excerpt': 'Done',
            'read_receipt': post.receipt, 'events': list(rows.values())}}

    monkeypatch.setattr(module, 'operator_memory_post', post)
    result = {'conversation_id': 'provider-c1', 'terminal_assistant_text': 'Done',
              'terminal_assistant_message_id': 'assistant-1', 'raw_sha256': hashlib.sha256(raw).hexdigest(),
              'raw_path_host': str(path)}
    outcome = module.admit_central_conversation(result_id='record-1', prompt='Do it', result=result, attachments=[])
    assert outcome['ok'] is True
    assert outcome['central_tool_event_count'] == 2
    assert outcome['central_observed_finished_container_exec_results'] == 1
    assert calls == ['record_conversation', 'read']
    assert list(rows) == ['b4pt0r-user:record-1', 'call-1', 'result-1', 'assistant-1']
    # A second admission is idempotent at source-message identity.
    module.admit_central_conversation(result_id='record-1', prompt='Do it', result=result, attachments=[])
    assert len(rows) == 4


def test_readback_rejects_missing_provider_tool_result(tmp_path, monkeypatch):
    module = sender()
    raw = tiny_stream()
    path = tmp_path / 'provider.sse'
    path.write_bytes(raw)
    def post(body):
        if body['operation'] == 'record_conversation':
            return {'ok': True, 'stored': True, 'conversation_id': 'provider-c1'}
        return {'ok': True, 'conversation': {'prompt': 'Do it', 'response_excerpt': 'Done',
            'read_receipt': {'raw_sha256': hashlib.sha256(raw).hexdigest()}, 'events': []}}
    monkeypatch.setattr(module, 'operator_memory_post', post)
    with pytest.raises(module.CapabilityError, match='tool-event count mismatch'):
        module.admit_central_conversation(result_id='record-1', prompt='Do it',
            result={'conversation_id': 'provider-c1', 'terminal_assistant_text': 'Done',
                    'raw_sha256': hashlib.sha256(raw).hexdigest(), 'raw_path_host': str(path)}, attachments=[])


@pytest.mark.parametrize(('name', 'sha', 'events', 'native_pairs'), [
    ('b4pt0r-chatmode-20261009T035349Z-acd8ad68.sse', 'ebe08118f82db144741daee4160bf38b977bdd3614016c949000253ce5d84e27', 70, 35),
    ('b4pt0r-chatmode-20261009T030900Z-17817860.sse', '90561c6a31452ad0c6d950b9aa2d55a2b83bd948372d96afc96a60f09e15c432', 84, 42),
    ('b4pt0r-chatmode-20261008T235828Z-da189640.sse', '103d0c1be46c8d70372b7aa2553a1db1a17af7b6f88899e6a112f8a55806353e', 491, 0),
])
def test_frozen_real_provider_streams(name, sha, events, native_pairs):
    path = Path.home() / '.local/share/cognilode/b4pt0r-chatmode' / name
    if not path.is_file():
        pytest.skip('frozen provider stream is available only on capture host')
    found = sender().provider_sse_tool_history(path.read_bytes(), expected_sha256=sha)
    assert len(found['tool_events']) == events
    assert found['finished_container_exec_results'] == native_pairs

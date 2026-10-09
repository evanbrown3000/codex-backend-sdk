import hashlib
from datetime import datetime, timedelta, timezone
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "scripts" / filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


upstream = load("decisionx_successor_packet_test", "cognilode-decisionx-successor")
native = load("decisionx_successor_native_test", "decisionx_successor_native.py")
handoff = load("decisionx_successor_handoff_retry_test", "cognilode-decisionx-advice-handoff")


def test_successor_retrieves_only_sources_available_before_target_provider_turn():
    cutoff = upstream.target_cutoff({
        'created_at': '2026-01-01T00:00:00Z',
        'claimed_at': '2026-02-01T00:00:00Z',
        'completed_at': '2026-03-01T00:00:00Z'})
    assert cutoff == datetime(2026, 2, 1, tzinfo=timezone.utc)
    selected = [
        {'segment_id': f'segment-{name}', 'provider': 'openai-codex',
         'conversation_id': name} for name in ('future', 'past')]

    def post(body):
        cid = body.get('conversation_id')
        if body['operation'] == 'read_segment':
            return {'segment': {'segment_id': body['segment_id'], 'kind': 'IAE',
                    'origin': 'chatgpt.com:decisionx_iae_label', 'content': '{}',
                    'metadata': {'drive_verified_source': True,
                                 'full_source_prompt_sha256': '1'*64,
                                 'full_source_response_sha256': '2'*64}}}
        assert body['operation'] == 'read'
        return {'conversation': {'provider': 'openai-codex', 'conversation_id': cid,
                'prompt_sha256': '1'*64, 'response_sha256': '2'*64,
                'events': [{'role': 'user', 'content': 'Do the work',
                            'created_at': '2026-01-01T00:00:00Z'},
                           {'role': 'assistant', 'content': 'Completed',
                            'created_at': '2026-03-01T00:00:00Z' if cid == 'future'
                                          else '2026-01-02T00:00:00Z'}],
                'capture': {'source_sha256': '3'*64}}}

    class Bridge:
        @staticmethod
        def _complete_source(index, _read):
            return datetime(2026, 1, 1, tzinfo=timezone.utc)

        @staticmethod
        def _source_fidelity(_capture):
            return "drive_verified_text_projection"

    refs, candidates = upstream.source_refs(post, selected, Bridge(), as_of=cutoff, limit=1)
    assert [row['conversation_id'] for row in refs] == ['past']
    assert [row['conversation_id'] for row in candidates] == ['past']
    assert refs[0]['source_fidelity'] == 'drive_verified_text_projection'


def test_future_successor_render_exposes_source_fidelity_without_changing_legacy_render():
    source = {'provider':'chatgpt-export-format','conversation_id':'history-1',
              'source_fidelity':'unverified_origin_rendered_text',
              'source_kind':'historical_s3_rendered',
              'source_provenance':'chatgpt_export_render_format_unverified_origin',
              'events':[{'role':'user','content':'A historical instruction'}]}
    assert 'Source fidelity:' not in native.render(source)
    rendered = native.render(source,include_fidelity=True)
    assert 'Source fidelity: unverified_origin_rendered_text' in rendered
    assert 'Source provenance: chatgpt_export_render_format_unverified_origin' in rendered
    assert 'does not establish original media or tool completeness' in rendered


class MissingSegmentHTTP(RuntimeError):
    code = "central_memory_http_failed"

    def __init__(self):
        super().__init__('Agent Memory HTTP 404: {"ok":false,"error":"segment_not_found"}')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def test_candidate_stock_gate_precedes_use_of_any_retrieved_hit():
    calls = []

    def post(body):
        calls.append(body)
        return {"ok": True, "ready": False, "distinct_sources": 499,
                "candidates": [{"segment_id": "not_eligible"}]}

    with pytest.raises(ValueError, match="below 500"):
        upstream.candidates(post, {"events": [{"role": "user", "content": "research conversation history"}]})
    assert len(calls) == 1 and calls[0]["operation"] == "decisionx_candidates"


def test_candidate_fusion_favors_user_intent_over_repeated_assistant_boilerplate():
    target = {"events": [
        {"role": "user", "content": "Restore biological cadence for chatmode rotor"},
        {"role": "assistant", "content": "Boilerplate progress progress progress progress progress"}]}
    rows = {
        "biological": [("intent", "old-intent")],
        "cadence": [("intent", "old-intent")],
        "chatmode": [("intent", "old-intent")],
        "progress": [("assistant", "old-boilerplate")],
        "boilerplate": [("assistant", "old-boilerplate")],
    }
    def post(body):
        result = []
        for _, cid in rows.get(body["q"], []):
            result.append({"segment_id": cid, "rank": -100 if cid == "old-boilerplate" else -1,
                           "source_key": "openai-codex:" + cid,
                           "content": json.dumps({"source_locator": {
                               "provider": "openai-codex", "conversation_id": cid}}),
                           "metadata": {"drive_verified_source": True}})
        return {"ok": True, "ready": True, "distinct_sources": 600, "candidates": result}
    _, selected = upstream.candidates(post, target)
    assert [item["conversation_id"] for item in selected] == ["old-intent"]
    assert set(selected[0]["query_evidence"]["intent"]) == {
        "biological", "cadence", "chatmode"}


def test_native_episode_cards_are_exact_ordered_source_turns():
    source = {"events": [
        {"id": "u1", "role": "user", "content": "Build the transport"},
        {"id": "a1", "role": "assistant", "content": "Transport deployed"},
        {"id": "u2", "role": "user", "content": "It failed under load; investigate"},
        {"id": "a2", "role": "assistant", "content": "Found connection leak"},
        {"id": "u3", "role": "user", "content": "The leak is fixed now"}]}
    descriptor = {"source_locator": {
        "provider": "openai-codex", "conversation_id": "older",
        "intent_turn_id": "u1", "action_turn_ids": ["a1"],
        "evaluation_turn_id": "u2", "following_action_turn_ids": ["a2"],
        "following_evaluation_turn_id": "u3"}}
    candidate = {"provider": "openai-codex", "conversation_id": "older",
                 "segment_id": "dx-older", "descriptor": descriptor}
    cards = native.evidence_cards([candidate], {("openai-codex", "older"): source})
    assert cards.index("Build the transport") < cards.index("Transport deployed")
    assert cards.index("It failed under load") < cards.index("Found connection leak")
    assert cards.index("Found connection leak") < cards.index("The leak is fixed")
    broken = json.loads(json.dumps(candidate))
    broken["descriptor"]["source_locator"]["evaluation_turn_id"] = "u3"
    with pytest.raises(ValueError, match="absent or out of order"):
        native.evidence_cards([broken], {("openai-codex", "older"): source})


def test_unattended_tick_does_not_enqueue_or_scan_stock_below_descriptor_gate(tmp_path):
    root_state = tmp_path / "root-cycle.json"
    root_state.write_text(json.dumps({"phase": "complete", "root_job_id": "root-job"}))
    calls = []

    def post(body):
        calls.append(body)
        return {"ok": True, "ready": False, "distinct_sources": 42}

    result = upstream.tick(post, output_root=tmp_path / "successor", root_state_path=root_state)
    assert result == {"ok": False, "reason": "shared_descriptor_index_below_500",
                      "distinct_sources": 42}
    assert [body["operation"] for body in calls] == ["decisionx_candidates"]


@pytest.mark.parametrize("root", [
    {"phase": "complete", "root_job_id": "new-root"},
    {"phase": "await_root", "root_job_id": "new-root"},
])
def test_new_root_does_not_orphan_older_uninstalled_successor(tmp_path, root):
    root_state = tmp_path / "root.json"
    root_state.write_text(json.dumps(root))
    output = tmp_path / "successor"
    output.mkdir()
    (output / "state.json").write_text(json.dumps({
        "target_job_id": "old-root", "job_id": "decisionx-successor-old"}))
    calls = []

    def post(body):
        calls.append(body)
        assert body == {"operation": "get_job", "job_id": "decisionx-successor-old"}
        return {"ok": True, "job": {"id": "decisionx-successor-old", "state": "queued"}}

    result = upstream.tick(post, output_root=output, root_state_path=root_state,
                           installed=lambda _job_id: False)
    assert result["phase"] == "already_queued"
    assert result["target_job_id"] == "old-root"
    assert result["job_id"] == "decisionx-successor-old"
    assert len(calls) == 1


def test_new_root_waits_only_until_previous_handoff_is_active(tmp_path):
    root_state = tmp_path / "root.json"
    root_state.write_text(json.dumps({"phase": "complete", "root_job_id": "new-root"}))
    output = tmp_path / "successor"
    output.mkdir()
    (output / "state.json").write_text(json.dumps({
        "target_job_id": "old-root", "job_id": "decisionx-successor-old"}))
    calls = []

    def post(body):
        calls.append(body)
        assert body["operation"] == "decisionx_candidates"
        return {"ok": True, "ready": False, "distinct_sources": 499}

    result = upstream.tick(post, output_root=output, root_state_path=root_state,
                           installed=lambda job_id: job_id == "decisionx-successor-old")
    assert result["reason"] == "shared_descriptor_index_below_500"
    assert len(calls) == 1


def test_native_reconstructs_exact_full_sources_and_rejects_tampering(tmp_path):
    target_id = "root-job-1"
    target = {"provider": "chatgpt.com", "conversation_id": "target-c",
              "prompt_sha256": "1" * 64, "response_sha256": "2" * 64,
              "events": [{"role": "user", "content": "Improve the DecisionX response"},
                         {"role": "assistant", "content": "Existing plan"}]}
    source = {"provider": "openai-codex", "conversation_id": "source-c",
              "prompt_sha256": "3" * 64, "response_sha256": "4" * 64,
              "capture": {"source_sha256": "5" * 64},
              "events": [{"id": "u1", "role": "user", "content": "Original request"},
                         {"id": "a1", "role": "assistant", "content": "Historical action"},
                         {"id": "u2", "role": "user", "content": "Historical outcome was good"}]}
    ref = {"provider": source["provider"], "conversation_id": source["conversation_id"],
           "prompt_sha256": source["prompt_sha256"], "response_sha256": source["response_sha256"],
           "source_sha256": "5" * 64, "source_at_utc": "2024-01-01T00:00:00+00:00"}
    candidate = {k: ref[k] for k in ("provider", "conversation_id", "prompt_sha256",
                                     "response_sha256", "source_sha256")}
    candidate.update(segment_id="s1", descriptor={"i": "Original request", "a": "Historical action",
        "source_locator": {"provider": "openai-codex", "conversation_id": "source-c",
                           "intent_turn_id": "u1", "action_turn_ids": ["a1"],
                           "evaluation_turn_id": "u2", "following_action_turn_ids": [],
                           "following_evaluation_turn_id": None}})
    target_raw, candidate_raw, source_raw = map(native.canonical, (target, [candidate], source))
    manifest = {"schema": "cognilode.decisionx.successor_input.v1",
                "evidence_contract_version": 2,
                "target_job_id": target_id, "target_provider": target["provider"],
                "target_conversation_id": target["conversation_id"],
                "target_prompt_sha256": target["prompt_sha256"],
                "target_response_sha256": target["response_sha256"],
                "target_json_sha256": digest(target_raw),
                "candidates_json_sha256": digest(candidate_raw),
                "source_refs": [ref], "source_refs_sha256": digest(native.canonical([ref]))}
    first, historical = tmp_path / "TARGET_AND_CANDIDATES.zip", tmp_path / "historical.zip"
    with zipfile.ZipFile(first, "w") as archive:
        archive.writestr("MANIFEST.json", native.canonical(manifest))
        archive.writestr("target.json", target_raw)
        archive.writestr("candidates.json", candidate_raw)
    part = {"provider": source["provider"], "conversation_id": source["conversation_id"],
            "prompt_sha256": source["prompt_sha256"],
            "response_sha256": source["response_sha256"],
            "part_index": 0, "part_count": 1, "source_json_sha256": digest(source_raw),
            "part_sha256": digest(source_raw), "path": "parts/0.part"}
    with zipfile.ZipFile(historical, "w") as archive:
        archive.writestr("MANIFEST.json", native.canonical({
            "schema": "cognilode.root_memory_sources.v1", "parts": [part]}))
        archive.writestr("parts/0.part", source_raw)
    result = native.run(first, [historical], tmp_path / "work")
    assert result["input_zip_sha256s"] == [digest(first.read_bytes()), digest(historical.read_bytes())]
    assert result["source_refs_sha256"] == manifest["source_refs_sha256"]
    assert "Historical action" in (tmp_path / "work/HISTORICAL_RENDERED.md").read_text()
    assert "Historical outcome was good" in (tmp_path / "work/SOURCE_EPISODES.md").read_text()
    assert result["source_episodes_sha256"] == digest((tmp_path / "work/SOURCE_EPISODES.md").read_bytes())
    proposed = "Use the independently verified historical conversation to choose the next research step and measure its external effect."
    (tmp_path / "work/DECISIONX_ADVICE.json").write_text(json.dumps({
        "schema": "cognilode.decisionx.successor_advice.v1",
        "target_job_id": target_id, "candidate_refs": [candidate],
        "proposed_next_instruction": proposed}))
    (tmp_path / "work/successor.plan").write_text("- [ ] " + proposed)
    (tmp_path / "work/EXTERNAL_EFFECT_INSTRUCTIONS.md").write_text("Install plan after verification.")
    sealed = native.finish(tmp_path / "work", tmp_path / "decisionx-successor-work-product.zip")
    with zipfile.ZipFile(sealed["output"]) as archive:
        output_manifest = json.loads(archive.read("MANIFEST.json"))
        assert output_manifest["members"]["NATIVE_COMPUTE.json"] == digest(
            archive.read("NATIVE_COMPUTE.json"))
    with zipfile.ZipFile(historical, "w") as archive:
        archive.writestr("MANIFEST.json", native.canonical({
            "schema": "cognilode.root_memory_sources.v1", "parts": [part]}))
        archive.writestr("parts/0.part", b"tampered")
    with pytest.raises(ValueError, match="source part SHA mismatch"):
        native.run(first, [historical], tmp_path / "work-again")


def test_enqueue_requires_exact_shared_queue_readback(monkeypatch):
    class FakeBuilder:
        @staticmethod
        def publish_private(row):
            return "https://example.invalid/" + row["sha256"]

    built = {"builder": FakeBuilder(), "identity": "abc123",
             "packets": [{"path": "/tmp/target.zip", "sha256": "a" * 64}],
             "manifest": {"target_job_id": "root-job", "candidate_refs": [{}],
                          "source_refs": [{}]}}
    calls = []

    def post(body):
        calls.append(body)
        if body["operation"] == "enqueue_decisionx_prompt":
            assert body["decisionx"]["target_job_id"] == "root-job"
            assert body["decisionx"]["reasoning_effort"] == "xhigh"
            assert body["decisionx"]["research_employee"] == "Nadia Brooks"
            assert body["decisionx"]["external_employee"] == "Rina Hale"
            return {"ok": True}
        return {"job": {"id": "decisionx-successor-abc123", "prompt_sha256": "0" * 64,
                        "state": "queued", "attachment_refs": body.get("attachment_refs", [])}}

    with pytest.raises(ValueError, match="D1 readback mismatch"):
        upstream.enqueue(post, built)
    assert [row["operation"] for row in calls] == ["enqueue_decisionx_prompt", "get_job"]


def test_failed_successor_is_deferred_then_reenqueued_with_new_fence(tmp_path, monkeypatch):
    root_state = tmp_path / "root.json"
    root_state.write_text(json.dumps({"phase": "complete", "root_job_id": "root-job"}))
    output = tmp_path / "successor"
    output.mkdir()
    state_path = output / "state.json"
    state_path.write_text(json.dumps({"schema": "cognilode.decisionx.successor_state.v1",
                                      "target_job_id": "root-job",
                                      "job_id": "decisionx-successor-old", "attempt": 0}))
    def post(body):
        if body["operation"] == "get_job":
            return {"ok": True, "job": {"id": "decisionx-successor-old", "state": "failed",
                                         "last_error": "provider artifact unusable"}}
        if body["operation"] == "decisionx_candidates":
            return {"ok": True, "ready": True, "distinct_sources": 500}
        raise AssertionError(body)
    deferred = upstream.tick(post, output_root=output, root_state_path=root_state)
    assert deferred["phase"] == "retry_deferred"
    saved = json.loads(state_path.read_text())
    assert saved["failure_observed_job_id"] == "decisionx-successor-old"
    saved["retry_after"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    state_path.write_text(json.dumps(saved))
    class Bridge:
        @staticmethod
        def shared_stock_census(_post, minimum):
            return {"multi_year_ready": True, "distinct_complete": 500,
                    "span_days": 800}
    monkeypatch.setattr(upstream, "load", lambda *_args: Bridge())
    monkeypatch.setattr(upstream, "packet", lambda *_args, **_kwargs: {
        "manifest": {"source_refs_sha256": "a" * 64}, "identity": "new"})
    seen = []
    def enqueue(_post, _built, **kwargs):
        seen.append(kwargs)
        return {"ok": True, "job_id": "decisionx-successor-new-retry-1", "state": "queued"}
    monkeypatch.setattr(upstream, "enqueue", enqueue)
    queued = upstream.tick(post, output_root=output, root_state_path=root_state)
    assert queued["phase"] == "queued"
    assert seen[0]["attempt"] == 1
    assert seen[0]["predecessor_job_id"] == "decisionx-successor-old"
    assert json.loads(state_path.read_text())["job_id"] == "decisionx-successor-new-retry-1"


def test_normal_absent_rejection_http_404_does_not_block_completed_job(tmp_path):
    root_state = tmp_path / "root.json"
    root_state.write_text(json.dumps({"phase": "complete", "root_job_id": "root-job"}))
    output = tmp_path / "successor"
    output.mkdir()
    (output / "state.json").write_text(json.dumps({"target_job_id": "root-job",
                                                    "job_id": "decisionx-successor-good"}))
    def post(body):
        if body["operation"] == "get_job":
            return {"ok": True, "job": {"id": "decisionx-successor-good", "state": "complete"}}
        if body["operation"] == "read_segment":
            raise MissingSegmentHTTP()
        raise AssertionError(body)
    result = upstream.tick(post, output_root=output, root_state_path=root_state)
    assert result["phase"] == "already_queued"
    assert result["job_state"] == "complete"


def test_optional_segment_reader_does_not_mask_auth_http_404():
    class AuthHTTP(RuntimeError):
        code = "central_memory_http_failed"
    def post(_body):
        raise AuthHTTP('Agent Memory HTTP 404: {"error":"wrong_route"}')
    with pytest.raises(AuthHTTP):
        handoff.read_optional_segment(post, "decisionx.handoff_rejected.abc")


def test_complete_but_invalid_provider_zip_records_shared_rejection_then_retries(tmp_path, monkeypatch):
    root_state = tmp_path / "root.json"
    root_state.write_text(json.dumps({"phase": "complete", "root_job_id": "root-job"}))
    output = tmp_path / "successor"
    output.mkdir()
    (output / "state.json").write_text(json.dumps({"target_job_id": "root-job",
                                                    "job_id": "decisionx-successor-bad", "attempt": 0}))
    stored = {}
    job = {"id": "decisionx-successor-bad", "state": "complete",
           "conversation_id": "chat-bad", "effect_evidence": []}
    def post(body):
        if body["operation"] == "get_job":
            return {"ok": True, "job": job}
        if body["operation"] == "append_segments":
            for row in body["segments"]:
                stored[row["segment_id"]] = row
            return {"ok": True}
        if body["operation"] == "read_segment":
            row = stored.get(body["segment_id"])
            if row is None:
                raise MissingSegmentHTTP()
            return {"ok": bool(row), "segment": row}
        raise AssertionError(body)
    def invalid(*_args, **_kwargs):
        raise handoff.InvalidProviderDeliverable("successor plan contains a tautological probe")
    monkeypatch.setattr(handoff, "handoff", invalid)
    rejected = handoff.tick(post, job_id=job["id"], output_root=tmp_path,
                            state_root=tmp_path / "handoff-state")
    assert rejected["phase"] == "provider_result_rejected"
    assert rejected["rejection_segment_id"] in stored
    repeated = handoff.tick(post, job_id=job["id"], output_root=tmp_path,
                            state_root=tmp_path / "handoff-state")
    assert repeated["phase"] == "provider_result_rejected"
    assert repeated["rejection_segment_id"] == rejected["rejection_segment_id"]
    deferred = upstream.tick(post, output_root=output, root_state_path=root_state)
    assert deferred["phase"] == "retry_deferred"
    assert deferred["job_state"] == "provider_result_rejected"


def test_three_spaced_completed_job_validation_failures_write_d1_retry_receipt(tmp_path, monkeypatch):
    job = {"id": "decisionx-successor-uncertain", "state": "complete",
           "conversation_id": "chat-uncertain", "effect_evidence": []}
    stored = {}
    def post(body):
        if body["operation"] == "get_job":
            return {"ok": True, "job": job}
        if body["operation"] == "read_segment":
            row = stored.get(body["segment_id"])
            if row is None:
                raise MissingSegmentHTTP()
            return {"ok": bool(row), "segment": row, "error": None if row else "segment_not_found"}
        if body["operation"] == "append_segments":
            for row in body["segments"]:
                stored[row["segment_id"]] = row
            return {"ok": True}
        raise AssertionError(body)
    def inconclusive(*_args, **_kwargs):
        raise ValueError("uploaded historical source no longer matches complete Drive readback")
    monkeypatch.setattr(handoff, "handoff", inconclusive)
    state_root = tmp_path / "handoff-state"
    for count in (1, 2):
        result = handoff.tick(post, job_id=job["id"], output_root=tmp_path,
                              state_root=state_root)
        assert result["phase"] == "validation_retry"
        assert result["observations"] == count
        retry_path = next(state_root.glob("*.validation.json"))
        value = json.loads(retry_path.read_text())
        value["last_counted_at"] = "2024-01-01T00:00:00+00:00"
        retry_path.write_text(json.dumps(value))
    rejected = handoff.tick(post, job_id=job["id"], output_root=tmp_path,
                            state_root=state_root)
    assert rejected["phase"] == "provider_result_rejected"
    assert rejected["observations"] == 3
    assert stored[rejected["rejection_segment_id"]]["metadata"]["reason"].startswith(
        "handoff_validation_unrecoverable:")

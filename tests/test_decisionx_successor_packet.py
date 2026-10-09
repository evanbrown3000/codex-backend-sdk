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


def test_native_reconstructs_exact_full_sources_and_rejects_tampering(tmp_path):
    target_id = "root-job-1"
    target = {"provider": "chatgpt.com", "conversation_id": "target-c",
              "prompt_sha256": "1" * 64, "response_sha256": "2" * 64,
              "events": [{"role": "user", "content": "Improve the DecisionX response"},
                         {"role": "assistant", "content": "Existing plan"}]}
    source = {"provider": "openai-codex", "conversation_id": "source-c",
              "prompt_sha256": "3" * 64, "response_sha256": "4" * 64,
              "capture": {"source_sha256": "5" * 64},
              "events": [{"role": "user", "content": "Original request"},
                         {"role": "assistant", "content": "Historical action"}]}
    ref = {"provider": source["provider"], "conversation_id": source["conversation_id"],
           "prompt_sha256": source["prompt_sha256"], "response_sha256": source["response_sha256"],
           "source_sha256": "5" * 64, "source_at_utc": "2024-01-01T00:00:00+00:00"}
    candidate = {k: ref[k] for k in ("provider", "conversation_id", "prompt_sha256",
                                     "response_sha256", "source_sha256")}
    candidate.update(segment_id="s1", descriptor={"i": "Original request", "a": "Historical action"})
    target_raw, candidate_raw, source_raw = map(native.canonical, (target, [candidate], source))
    manifest = {"schema": "cognilode.decisionx.successor_input.v1",
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
            return {"ok": bool(row), "segment": row}
        raise AssertionError(body)
    def invalid(*_args, **_kwargs):
        raise handoff.InvalidProviderDeliverable("successor plan contains a tautological probe")
    monkeypatch.setattr(handoff, "handoff", invalid)
    rejected = handoff.tick(post, job_id=job["id"], output_root=tmp_path,
                            state_root=tmp_path / "handoff-state")
    assert rejected["phase"] == "provider_result_rejected"
    assert rejected["rejection_segment_id"] in stored
    deferred = upstream.tick(post, output_root=output, root_state_path=root_state)
    assert deferred["phase"] == "retry_deferred"
    assert deferred["job_state"] == "provider_result_rejected"

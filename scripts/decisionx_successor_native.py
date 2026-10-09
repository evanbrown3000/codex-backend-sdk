#!/usr/bin/env python3
"""Run inside the ChatGPT Chat-mode sandbox on physically uploaded source ZIPs.

This script verifies the complete historical rows and target, then renders all
evidence for semantic comparison by the hosted agent. It does no host work.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def render(row: dict, *, include_fidelity: bool = False) -> str:
    lines = [f"# {row['provider']} / {row['conversation_id']}"]
    if include_fidelity:
        capture = row.get("capture") or {}
        lines.extend([
            "Source fidelity: " + str(row.get("source_fidelity") or "unclassified_text_projection"),
            "Source kind: " + str(row.get("source_kind") or capture.get("source_kind") or "unknown"),
            "Source provenance: " + str(row.get("source_provenance") or capture.get("source_provenance") or "unverified"),
            "Rendered text does not establish original media or tool completeness."])
    for event in row.get("events") or []:
        if event.get("role") in {"user", "assistant", "developer", "system"}:
            lines.append("\n## " + str(event["role"]) + "\n" + str(event.get("content") or ""))
    return "\n".join(lines)


def evidence_cards(candidates: list[dict], historical: dict[tuple[str, str], dict]) -> str:
    """Bind each agent-derived IAE/IAI label to its actual ordered source turns.

    The label is only a search descriptor. Its I/A/E prose is never substituted
    for the original turns when the hosted analyst compares possible actions.
    """
    cards = []
    fields = (("intent_turn_id", "user", False),
              ("action_turn_ids", "assistant", True),
              ("evaluation_turn_id", "user", False),
              ("following_action_turn_ids", "assistant", True),
              ("following_evaluation_turn_id", "user", False))
    for candidate in candidates:
        key = candidate["provider"], candidate["conversation_id"]
        row = historical[key]
        ordinary = []
        for event in row.get("events") or []:
            role = str(event.get("role") or "").lower()
            content = event.get("content")
            if isinstance(content, list):
                content = "\n".join(str(item.get("text") or "") for item in content
                                    if isinstance(item, dict))
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                ordinary.append({"id": str(event.get("id") or event.get("index") or len(ordinary)),
                                 "role": role, "content": content})
        locator = (candidate.get("descriptor") or {}).get("source_locator") or {}
        if (locator.get("provider"), locator.get("conversation_id")) != key:
            raise ValueError("candidate source locator differs from complete source")
        cursor = -1
        used = []
        for field, role, multiple in fields:
            ids = locator.get(field) or ([] if multiple else None)
            if multiple and not isinstance(ids, list):
                raise ValueError("candidate turn locator malformed")
            if not multiple:
                ids = [ids] if ids is not None else []
            if field in {"intent_turn_id", "action_turn_ids"} and not ids:
                raise ValueError("candidate I/A source turns missing")
            for turn_id in ids:
                found = next((index for index in range(cursor + 1, len(ordinary))
                              if ordinary[index]["id"] == str(turn_id)
                              and ordinary[index]["role"] == role), None)
                if found is None:
                    raise ValueError("candidate I/A/E turn absent or out of order in complete source")
                cursor = found
                used.append((field, ordinary[found]))
        cards.extend([f"# {candidate['segment_id']} / {key[0]} / {key[1]}",
                      "Agent-derived descriptor is a retrieval hint; source turns below are evidence.",
                      ""])
        for field, turn in used:
            cards.extend([f"## {field} / {turn['role']} / {turn['id']}", turn["content"], ""])
        if not locator.get("evaluation_turn_id"):
            cards.extend(["## evaluation missing", "No later user evaluation is present in this episode.", ""])
    return "\n".join(cards)


def run(first: Path, sources: list[Path], out: Path) -> dict:
    zip_shas = [sha(path.read_bytes()) for path in [first, *sources]]
    with zipfile.ZipFile(first) as archive:
        if archive.testzip() is not None:
            raise ValueError("target ZIP CRC failure")
        manifest = json.loads(archive.read("MANIFEST.json"))
        if manifest.get("schema") != "cognilode.decisionx.successor_input.v1":
            raise ValueError("input schema mismatch")
        target_raw = archive.read("target.json")
        if sha(target_raw) != manifest["target_json_sha256"]:
            raise ValueError("target SHA mismatch")
        target = json.loads(target_raw)
        if (target.get("provider"), target.get("conversation_id")) != (
                manifest["target_provider"], manifest["target_conversation_id"]):
            raise ValueError("target identity mismatch")
        if (target.get("prompt_sha256"), target.get("response_sha256")) != (
                manifest["target_prompt_sha256"], manifest["target_response_sha256"]):
            raise ValueError("target prompt/response mismatch")
        candidate_raw = archive.read("candidates.json")
        if sha(candidate_raw) != manifest["candidates_json_sha256"]:
            raise ValueError("candidate SHA mismatch")
        candidates = json.loads(candidate_raw)
    refs = manifest["source_refs"]
    if sha(canonical(refs)) != manifest["source_refs_sha256"]:
        raise ValueError("source refs SHA mismatch")
    if len(refs) != len({(r["provider"], r["conversation_id"]) for r in refs}):
        raise ValueError("duplicate historical source")
    parts = {}
    for packet in sources:
        with zipfile.ZipFile(packet) as archive:
            if archive.testzip() is not None:
                raise ValueError("source ZIP CRC failure")
            src_manifest = json.loads(archive.read("MANIFEST.json"))
            if src_manifest.get("schema") != "cognilode.root_memory_sources.v1":
                raise ValueError("source ZIP schema mismatch")
            for item in src_manifest["parts"]:
                chunk = archive.read(item["path"])
                if sha(chunk) != item["part_sha256"]:
                    raise ValueError("source part SHA mismatch")
                key = (item["provider"], item["conversation_id"])
                parts.setdefault(key, []).append((item["part_index"], item["part_count"],
                                                   item["source_json_sha256"], chunk))
    historical = {}
    for ref in refs:
        key = (ref["provider"], ref["conversation_id"])
        group = sorted(parts.get(key, []))
        if not group or len(group) != group[0][1] or [g[0] for g in group] != list(range(len(group))):
            raise ValueError("historical source incomplete")
        if len({g[2] for g in group}) != 1:
            raise ValueError("historical source part identity mismatch")
        raw = b"".join(g[3] for g in group)
        if sha(raw) != group[0][2]:
            raise ValueError("historical source row SHA mismatch")
        row = json.loads(raw)
        if (row.get("provider"), row.get("conversation_id"), row.get("prompt_sha256"),
            row.get("response_sha256"), (row.get("capture") or {}).get("source_sha256")) != (
                *key, ref["prompt_sha256"], ref["response_sha256"], ref["source_sha256"]):
            raise ValueError("historical source D1 identity mismatch")
        historical[key] = row
    if set(parts) != set(historical):
        raise ValueError("unlisted historical source")
    for candidate in candidates:
        if (candidate["provider"], candidate["conversation_id"]) not in historical:
            raise ValueError("candidate lacks full historical source")
    out.mkdir(parents=True, exist_ok=True)
    (out / "TARGET_RENDERED.md").write_text(render(target))
    with (out / "HISTORICAL_RENDERED.md").open("w") as stream:
        for ref in refs:
            stream.write(render(historical[(ref["provider"], ref["conversation_id"])],
                                include_fidelity=bool(ref.get("source_fidelity"))) + "\n\n")
    (out / "CANDIDATES.json").write_bytes(canonical(candidates))
    episode_sha = None
    if manifest.get("evidence_contract_version") == 2:
        (out / "SOURCE_EPISODES.md").write_text(evidence_cards(candidates, historical))
        episode_sha = sha((out / "SOURCE_EPISODES.md").read_bytes())
    compute = {"schema": "cognilode.decisionx.successor_native_compute.v1",
               "input_zip_sha256s": zip_shas,
               "target_job_id": manifest["target_job_id"],
               "candidate_count": len(candidates), "source_count": len(refs),
               "source_refs_sha256": manifest["source_refs_sha256"],
               "target_rendered_sha256": sha((out / "TARGET_RENDERED.md").read_bytes()),
               "historical_rendered_sha256": sha((out / "HISTORICAL_RENDERED.md").read_bytes())}
    if episode_sha is not None:
        compute["source_episodes_sha256"] = episode_sha
    (out / "NATIVE_COMPUTE.json").write_bytes(canonical(compute))
    return compute


def finish(work: Path, output: Path) -> dict:
    """Deterministically seal the analyst's four files into one verifiable ZIP."""
    names = ("DECISIONX_ADVICE.json", "successor.plan",
             "EXTERNAL_EFFECT_INSTRUCTIONS.md", "NATIVE_COMPUTE.json")
    contents = {name: (work / name).read_bytes() for name in names}
    advice = json.loads(contents["DECISIONX_ADVICE.json"])
    compute = json.loads(contents["NATIVE_COMPUTE.json"])
    if advice.get("schema") != "cognilode.decisionx.successor_advice.v1":
        raise ValueError("advice schema mismatch")
    if compute.get("schema") != "cognilode.decisionx.successor_native_compute.v1":
        raise ValueError("native compute schema mismatch")
    if advice.get("target_job_id") != compute.get("target_job_id"):
        raise ValueError("target job identity mismatch")
    proposed = str(advice.get("proposed_next_instruction") or "").strip()
    if len(proposed) < 80 or proposed not in contents["successor.plan"].decode():
        raise ValueError("substantive proposed instruction absent from successor.plan")
    if not advice.get("candidate_refs"):
        raise ValueError("source-linked candidate_refs absent")
    manifest = {"schema": "cognilode.decisionx.successor_output.v1",
                "members": {name: sha(raw) for name, raw in contents.items()}}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("MANIFEST.json", canonical(manifest))
        for name, raw in contents.items():
            archive.writestr(name, raw)
    if output.stat().st_size > 20 * 1024 * 1024:
        output.unlink()
        raise ValueError("successor work-product ZIP exceeds 20MiB")
    return {"output": str(output), "sha256": sha(output.read_bytes()),
            "members": manifest["members"]}


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--finish":
        print(json.dumps(finish(Path(sys.argv[2]), Path(sys.argv[3])), sort_keys=True))
        raise SystemExit(0)
    if len(sys.argv) < 4:
        raise SystemExit("usage: python RUN_ME.py TARGET_AND_CANDIDATES.zip OUTDIR SOURCE_ZIP...")
    print(json.dumps(run(Path(sys.argv[1]), [Path(p) for p in sys.argv[3:]], Path(sys.argv[2])),
                     sort_keys=True))

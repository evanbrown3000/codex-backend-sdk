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


def render(row: dict) -> str:
    lines = [f"# {row['provider']} / {row['conversation_id']}"]
    for event in row.get("events") or []:
        if event.get("role") in {"user", "assistant", "developer", "system"}:
            lines.append("\n## " + str(event["role"]) + "\n" + str(event.get("content") or ""))
    return "\n".join(lines)


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
            stream.write(render(historical[(ref["provider"], ref["conversation_id"])]) + "\n\n")
    (out / "CANDIDATES.json").write_bytes(canonical(candidates))
    compute = {"schema": "cognilode.decisionx.successor_native_compute.v1",
               "input_zip_sha256s": zip_shas,
               "target_job_id": manifest["target_job_id"],
               "candidate_count": len(candidates), "source_count": len(refs),
               "source_refs_sha256": manifest["source_refs_sha256"],
               "target_rendered_sha256": sha((out / "TARGET_RENDERED.md").read_bytes()),
               "historical_rendered_sha256": sha((out / "HISTORICAL_RENDERED.md").read_bytes())}
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

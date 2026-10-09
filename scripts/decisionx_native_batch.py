#!/usr/bin/env python3
"""Run source verification and lexical-neighbor computation inside Chat-mode sandbox.

This script travels INSIDE each attached DecisionX ZIP. The hosted agent runs it
there and uses its output while writing semantic I/A/E descriptors. It does not
contact the user's accounts or create a conversation store in the sandbox.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import zipfile

TERMS = re.compile(r"(?u)\b[\w][\w.-]{2,}\b")


def digest(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def full_sources(source_zips: list[Path]) -> tuple[dict[tuple[str, str], dict], list[str]]:
    if not source_zips:
        raise ValueError("complete source ZIP attachments are required")
    parts: dict[tuple[str, str], list[tuple[int, int, str, bytes]]] = {}
    digests = []
    for source_zip in source_zips:
        digests.append(digest(source_zip.read_bytes()))
        with zipfile.ZipFile(source_zip) as archive:
            if archive.testzip() is not None:
                raise ValueError("complete source ZIP CRC failed")
            manifest = json.loads(archive.read("MANIFEST.json"))
            if manifest.get("schema") != "cognilode.root_memory_sources.v1":
                raise ValueError("complete source manifest schema mismatch")
            for row in manifest["parts"]:
                raw = archive.read(row["path"])
                if digest(raw) != row["part_sha256"]:
                    raise ValueError("complete source part SHA mismatch")
                key = (row["provider"], row["conversation_id"])
                parts.setdefault(key, []).append((row["part_index"], row["part_count"],
                                                    row["source_json_sha256"], raw))
    sources = {}
    for key, group in parts.items():
        group.sort()
        if len(group) != group[0][1] or [row[0] for row in group] != list(range(group[0][1])):
            raise ValueError("complete source parts missing")
        if len({row[2] for row in group}) != 1:
            raise ValueError("complete source part hashes disagree")
        raw = b"".join(row[3] for row in group)
        if digest(raw) != group[0][2]:
            raise ValueError("complete source row SHA mismatch")
        source = json.loads(raw)
        if (source.get("provider"), source.get("conversation_id")) != key:
            raise ValueError("complete source identity mismatch")
        sources[key] = source
    return sources, digests


def run(source_zip: Path, output_dir: Path, source_zips: list[Path]) -> dict:
    raw_zip = source_zip.read_bytes()
    sources, source_zip_digests = full_sources(source_zips)
    with zipfile.ZipFile(source_zip) as archive:
        if archive.testzip() is not None:
            raise ValueError("input ZIP CRC failed")
        manifest = json.loads(archive.read("manifest.json"))
        rows = [json.loads(line) for line in archive.read("episodes.jsonl").decode().splitlines()
                if line.strip()]
    if manifest.get("schema") != "decisionx.iae.batch.v3" or len(rows) != manifest.get("episode_count"):
        raise ValueError("input manifest is inconsistent")
    expected = {item["episode_id"]: item["source_sha256"] for item in manifest["episodes"]}
    if len(expected) != len(rows):
        raise ValueError("duplicate manifest episode")
    term_docs = []
    for row in rows:
        source = {key: value for key, value in row.items() if key not in {"episode_id", "source_sha256"}}
        if expected.get(row["episode_id"]) != row["source_sha256"] or digest(canonical(source)) != row["source_sha256"]:
            raise ValueError("episode source SHA mismatch")
        key = (row["provider"], row["conversation_id"])
        full = sources.get(key)
        if full is None:
            raise ValueError("episode lacks attached complete source")
        messages = []
        for event in full.get("events") or []:
            role = str(event.get("role") or "").lower()
            content = event.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                messages.append((role, content))
        if ("user", row["intent_turn"]["text"]) not in messages or any(
                ("assistant", action["text"]) not in messages for action in row["action_turns"]):
            raise ValueError("episode text is absent from its complete source")
        text = "\n".join([row["intent_turn"]["text"],
                          *(item["text"] for item in row["action_turns"]),
                          (row.get("next_user_turn") or {}).get("text", "")])
        term_docs.append(Counter(TERMS.findall(text.casefold())))
    document_frequency = Counter(term for bag in term_docs for term in bag)
    neighbors = []
    for index, bag in enumerate(term_docs):
        scored = []
        for other, candidate in enumerate(term_docs):
            if index == other:
                continue
            shared = bag.keys() & candidate.keys()
            score = sum(min(bag[term], candidate[term]) / document_frequency[term]
                        for term in shared)
            if score > 0:
                scored.append((score, rows[other]["episode_id"]))
        scored.sort(key=lambda item: (-item[0], item[1]))
        neighbors.append({"episode_id": rows[index]["episode_id"],
                          "top_neighbors": [{"episode_id": key, "score": round(score, 6)}
                                            for score, key in scored[:5]],
                          "top_terms": [term for term, _ in bag.most_common(15)]})
    output_dir.mkdir(parents=True, exist_ok=True)
    neighbors_raw = canonical(neighbors) + b"\n"
    (output_dir / "neighbors.json").write_bytes(neighbors_raw)
    result = {"schema": "decisionx.native_batch_compute.v1",
              "input_zip_sha256": digest(raw_zip), "episode_count": len(rows),
              "distinct_episode_count": len(expected),
              "source_zip_sha256s": source_zip_digests,
              "distinct_complete_sources": len(sources),
              "source_refs": [{"provider": key[0], "conversation_id": key[1],
                               "prompt_sha256": source["prompt_sha256"],
                               "response_sha256": source["response_sha256"]}
                              for key, source in sorted(sources.items())],
              "distinct_term_count": len(document_frequency),
              "neighbors_sha256": digest(neighbors_raw)}
    (output_dir / "NATIVE_COMPUTE.json").write_bytes(canonical(result) + b"\n")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: python RUN_ME.py episode-input.zip output-dir complete-source-zip [more-source-zips...]")
    print(json.dumps(run(Path(sys.argv[1]), Path(sys.argv[2]),
                         [Path(value) for value in sys.argv[3:]]), sort_keys=True))

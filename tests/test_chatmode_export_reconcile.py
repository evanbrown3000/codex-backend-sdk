from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-chatmode-export-reconcile"
loader = SourceFileLoader("chatmode_export_reconcile_test", str(SCRIPT))
spec = spec_from_loader(loader.name, loader)
reconcile = module_from_spec(spec)
loader.exec_module(reconcile)


def conversation(cid, prompt, when, *, on_current_branch=True):
    user = {"id": "original-user-id", "author": {"role": "user"},
            "content": {"parts": [prompt]}, "create_time": when}
    assistant = {"id": "final", "author": {"role": "assistant"},
                 "content": {"parts": ["real response"]}}
    mapping = {"root": {"parent": None, "message": None},
               "user": {"parent": "root", "message": user},
               "final": {"parent": "user", "message": assistant}}
    if not on_current_branch:
        mapping["other"] = {"parent": "root", "message": {
            "id": "other", "author": {"role": "user"}, "content": {"parts": ["other"]}}}
    return {"id": cid, "create_time": when, "current_node": "final" if on_current_branch else "other",
            "mapping": mapping}


def test_bulk_shard_scan_binds_only_exact_current_branch_near_enqueue():
    prompt = "do the status research"
    t = datetime(2026, 10, 1, 21, 46, tzinfo=timezone.utc).timestamp()
    job = {"id": "legacy-one", "created_at": datetime.fromtimestamp(t, timezone.utc).isoformat(),
           "prompt": prompt, "prompt_sha256": sha256(prompt.encode()).hexdigest()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("conversations-000.json", json.dumps([
            conversation("too-old", prompt, t - 100000),
            conversation("off-branch", prompt, t + 90, on_current_branch=False)]))
        out.writestr("conversations-001.json", json.dumps([
            conversation("exact-conversation", prompt, t + 1800),
            conversation("wrong-prompt", "other", t + 1850)]))
    buffer.seek(0)
    with zipfile.ZipFile(buffer) as archive:
        matches, scanned = reconcile.scan(archive, [job])
    assert scanned == ["conversations-001.json", "conversations-000.json"]
    assert [(x["conversation_id"], x["user_message_id"], x["prompt_sha256"])
            for x in matches[job["id"]]] == [
        ("exact-conversation", "original-user-id", job["prompt_sha256"])]


def test_export_range_rejects_changed_etag_or_full_body(monkeypatch):
    import chatmode_export_range as module
    class Response:
        def __init__(self, status, headers, content=b""):
            self.status_code, self.headers, self.content = status, headers, content
        def raise_for_status(self):
            assert self.status_code == 200
    class Session:
        def head(self, *_args, **_kw):
            return Response(200, {"Content-Length": "8", "Accept-Ranges": "bytes", "ETag": '"a"'})
        def get(self, *_args, **_kw):
            return Response(200, {"ETag": '"b"'}, b"abcdefgh")
    source = module.AuthenticatedRangeFile(Session(), "https://chatgpt.com/export", chunk_size=4)
    try:
        source.read(1)
        assert False, "full-body fallback must be rejected"
    except module.ExportChanged:
        pass

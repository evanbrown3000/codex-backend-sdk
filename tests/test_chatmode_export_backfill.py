from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cognilode-chatmode-export-backfill"
loader = SourceFileLoader("chatmode_export_backfill_test", str(SCRIPT))
spec = spec_from_loader(loader.name, loader)
backfill = module_from_spec(spec)
loader.exec_module(backfill)


def test_text_projection_preserves_branch_edges_and_nontext_omission():
    source = {"mapping": {
        "root": {"parent": None, "message": None},
        "u": {"parent": "root", "message": {"id": "user-1", "author": {"role": "user"},
            "content": {"parts": ["Ask"]}, "create_time": 1}},
        "a": {"parent": "u", "message": {"id": "assistant-1", "author": {"role": "assistant"},
            "content": {"parts": ["Answer"]}, "create_time": 2}},
        "alternate": {"parent": "u", "message": {"id": "assistant-2", "author": {"role": "assistant"},
            "content": {"parts": ["Other branch"]}, "create_time": 3}},
        "media": {"parent": "a", "message": {"id": "media-1", "author": {"role": "user"},
            "content": {"content_type": "image_asset_pointer", "parts": []}, "create_time": 4}},
    }}
    events, summary = backfill.normalize(source)
    assert [e["content"] for e in events] == ["Ask", "Answer", "Other branch"]
    assert summary["source_nodes"] == 5
    assert summary["source_nontext_messages"] == 1
    assert ("alternate", "u") in summary["branch_parent_edges"]
    assert ("media", "a") in summary["branch_parent_edges"]


def test_old_source_collision_gets_versioned_id(monkeypatch):
    old = {"capture": {"source_conversation_sha256": "different", "export_etag": '"old"'}}
    monkeypatch.setattr(backfill, "prior_conversation", lambda cid: old if cid == "original" else None)
    identity, prior = backfill.store_identity("original", "current", SimpleNamespace(etag='"new"'))
    assert identity.startswith("original@export-")
    assert prior is None


def test_same_source_id_is_idempotent(monkeypatch):
    expected = {"capture": {"source_conversation_sha256": "current", "export_etag": '"new"'}}
    monkeypatch.setattr(backfill, "prior_conversation", lambda cid: expected)
    assert backfill.store_identity("original", "current", SimpleNamespace(etag='"new"')) == (
        "original", expected)

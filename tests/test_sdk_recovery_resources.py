from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types


RESOURCE = Path(__file__).resolve().parents[1] / "codex_backend_sdk" / "resources" / "chatgpt.py"


def load_resource_module():
    root = types.ModuleType("sdkfixture")
    root.__path__ = []
    resources = types.ModuleType("sdkfixture.resources")
    resources.__path__ = []
    sys.modules["sdkfixture"] = root
    sys.modules["sdkfixture.resources"] = resources

    models = types.ModuleType("sdkfixture._models")
    class ChatGPTSpeech:
        def __init__(self, *, content, content_type):
            self.content = content
            self.content_type = content_type
    models.ChatGPTSpeech = ChatGPTSpeech
    sys.modules["sdkfixture._models"] = models

    utils = types.ModuleType("sdkfixture._utils")
    utils._jsonable = lambda value: value
    sys.modules["sdkfixture._utils"] = utils

    for name, cls_name in (
        ("chatgpt_apps", "ChatGPTApps"),
        ("chatgpt_connectors", "ChatGPTConnectors"),
        ("chatgpt_plugins", "ChatGPTPlugins"),
        ("chatgpt_writing_blocks", "ChatGPTWritingBlocks"),
    ):
        module = types.ModuleType(f"sdkfixture.resources.{name}")
        setattr(module, cls_name, type(cls_name, (), {"__init__": lambda self, client: setattr(self, "_client", client)}))
        sys.modules[module.__name__] = module

    spec = importlib.util.spec_from_file_location("sdkfixture.resources.chatgpt", RESOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def conversation_fixture():
    return {
        "current_node": "assistant",
        "mapping": {
            "user": {
                "parent": None,
                "message": {
                    "id": "user-1",
                    "author": {"role": "user"},
                    "content": {"parts": ["hello"]},
                },
            },
            "assistant": {
                "parent": "user",
                "message": {
                    "id": "assistant-1",
                    "author": {"role": "assistant"},
                    "content": {"parts": ["done"]},
                    "end_turn": True,
                    "status": "finished_successfully",
                },
            },
        },
    }


def test_sdk_reconcile_turn_is_read_only_and_finds_exact_submission():
    mod = load_resource_module()
    class Client:
        def __init__(self): self.calls = []
        def _get_chatgpt(self, path, *, params=None):
            self.calls.append((path, params))
            return conversation_fixture()
    client = Client()
    resource = mod.ChatGPTConversations(client)

    result = resource.reconcile_turn("conv-1", "user-1")

    assert result["accepted"] is True
    assert result["terminal"] is True
    assert result["assistant_text"] == "done"
    assert client.calls == [("/conversation/conv-1", None)]


def test_sdk_reconcile_turn_reports_unseen_message_without_mutation():
    mod = load_resource_module()
    class Client:
        def _get_chatgpt(self, path, *, params=None):
            return conversation_fixture()
    result = mod.ChatGPTConversations(Client()).reconcile_turn("conv-1", "other-user")
    assert result == {"accepted": False, "terminal": False, "conversation_id": "conv-1"}


def test_sdk_interpreter_download_uses_read_only_metadata_then_durable_file(tmp_path):
    mod = load_resource_module()
    class DownloadResponse:
        content = b"artifact"
    class Client:
        def __init__(self): self.calls = []
        def _get_chatgpt(self, path, *, params=None):
            self.calls.append(("GET", path, params))
            return {"download_url": "https://example.invalid/file"}
        def _download_chatgpt_link(self, url):
            self.calls.append(("DOWNLOAD", url))
            return DownloadResponse()
    client = Client()
    destination = tmp_path / "artifact.txt"

    result = mod.ChatGPTFiles(client).download_interpreter_artifact(
        "conv-1", "assistant-1", "/mnt/data/artifact.txt",
        response_format="file", output_path=destination,
    )

    assert result == destination
    assert destination.read_bytes() == b"artifact"
    assert client.calls[0] == (
        "GET",
        "/conversation/conv-1/interpreter/download",
        {"message_id": "assistant-1", "sandbox_path": "/mnt/data/artifact.txt"},
    )

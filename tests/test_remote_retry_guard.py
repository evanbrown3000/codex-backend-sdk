from pathlib import Path


def test_remote_wrapper_disables_automatic_sdk_retries_for_chatgpt_mutations():
    source = (Path(__file__).resolve().parents[1] / "codex_backend_sdk" / "cognilode_remote.py").read_text(encoding="utf-8")
    assert "max_retries=1" not in source
    assert source.count("max_retries=0") == 2

"""In-memory access to Cognilode operator custody.

The SDK never requires a provider credential. A launcher may pass the operator
capability directly or through an inherited descriptor such as
``/proc/self/fd/N``. Missing descriptors are treated as absent capabilities,
not as startup failures.
"""

from __future__ import annotations

import os
from pathlib import Path


def operator_token(explicit: str | None = None) -> str:
    value = (
        explicit
        or os.environ.get("COGNILODE_OPERATOR_TOKEN")
        or os.environ.get("COGNILODE_CENTRAL_ACCESS_TOKEN")
        or ""
    ).strip()
    if value:
        return value.removeprefix("Bearer ").strip()

    descriptor = os.environ.get("COGNILODE_OPERATOR_TOKEN_FILE", "").strip()
    if not descriptor:
        return ""
    try:
        return Path(descriptor).read_text(encoding="utf-8").strip().removeprefix("Bearer ").strip()
    except OSError:
        return ""

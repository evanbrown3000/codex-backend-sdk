"""Small capability discovery primitive shared by agent integrations.

This is intentionally a local composition layer, not a control plane. It gives
agents a stable way to publish and discover existing execution surfaces without
rebuilding provider, transport, or memory implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable


@dataclass(frozen=True)
class Capability:
    name: str
    interface: str
    owner: str
    consumers: tuple[str, ...] = ()
    evidence: str = ""


class CapabilityRegistry:
    """In-process registry for capability handoff.

    Persistence and ownership remain with the caller. This deliberately avoids
    creating another database or queue.
    """

    def __init__(self, capabilities: Iterable[Capability] = ()):
        self._capabilities = {item.name: item for item in capabilities}

    def register(self, capability: Capability) -> Capability:
        self._capabilities[capability.name] = capability
        return capability

    def find(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def search(self, term: str = "") -> list[dict]:
        term = term.lower()
        return [
            asdict(item)
            for item in self._capabilities.values()
            if not term or term in item.name.lower() or term in item.interface.lower()
        ]

    def export(self) -> list[dict]:
        return [asdict(item) for item in self._capabilities.values()]

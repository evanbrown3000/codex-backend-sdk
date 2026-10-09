"""Recurring successor engine from the Chat-mode work product.

The adapters own TaskFlow discovery, RPE planning and the hosted D1 queue.
In particular, a queue adapter must atomically fence replacement against the
latest hosted heartbeat and provider-send custody. A local timeout alone is
never evidence that a ChatGPT send can be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any


@dataclass(frozen=True)
class WorkItem:
    project: str
    step: str
    state: str
    priority: float = 0.0


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).timestamp()
        except ValueError:
            pass
    return None


class SuccessorEngine:
    """Drive one recurring planning cycle through supplied live adapters.

    ``queue.running()`` must include claimed and effect-pending hosted jobs.
    ``queue.replace_if_stalled(job_id, heartbeat, age)`` must compare-and-swap
    the current queue row, confirm no provider-send custody exists, and create
    at most one successor. It must return the replacement or None. The engine
    deliberately has no separate cancel and submit calls for a running job.
    """

    def __init__(self, store: Any, rpe: Any, queue: Any, *, min_inflight: int = 10,
                 stall_seconds: int = 900, max_submissions_per_tick: int = 10,
                 clock: Any = time.time):
        if min_inflight < 1 or stall_seconds < 1 or max_submissions_per_tick < 1:
            raise ValueError("engine limits must be positive")
        self.store = store
        self.rpe = rpe
        self.queue = queue
        self.min_inflight = min_inflight
        self.stall_seconds = stall_seconds
        self.max_submissions_per_tick = max_submissions_per_tick
        self.clock = clock

    def discover(self) -> list[WorkItem]:
        return [WorkItem(**row) for row in self.store.unfinished_steps()]

    def replace_stalled(self) -> int:
        replacements = 0
        now = self.clock()
        for job in self.queue.running():
            heartbeat = _epoch(getattr(job, "heartbeat_at", None))
            started = _epoch(getattr(job, "started_at", None))
            # A missing heartbeat is an unknown state, not permission to replay.
            if heartbeat is None or started is None or heartbeat < started:
                continue
            if now - heartbeat <= self.stall_seconds:
                continue
            if self.queue.replace_if_stalled(job.id, heartbeat, self.stall_seconds):
                replacements += 1
        return replacements

    def successor(self) -> dict[str, int]:
        replacements = self.replace_stalled()
        submitted = 0
        # Re-read hosted occupancy after every submission. An unchanged read
        # must not cause an unbounded stream of duplicate plans in one tick.
        while submitted < self.max_submissions_per_tick:
            occupancy = len(self.queue.running())
            if occupancy >= self.min_inflight:
                break
            items = self.discover()
            if not items:
                break
            plan = self.rpe.choose_successor([item.__dict__ for item in items])
            if not plan:
                break
            if not self.queue.submit(plan):
                break
            submitted += 1
            if len(self.queue.running()) <= occupancy:
                break
        return {"submitted": submitted, "replaced": replacements}

    def run_forever(self, interval: int = 60) -> None:
        if interval < 1:
            raise ValueError("interval must be positive")
        while True:
            self.successor()
            time.sleep(interval)

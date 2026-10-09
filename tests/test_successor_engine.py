from dataclasses import dataclass

from successor_engine import SuccessorEngine


@dataclass
class Job:
    id: str
    started_at: float
    heartbeat_at: float | None


class Store:
    def unfinished_steps(self):
        return [{"project": "live", "step": "next", "state": "ready"}]


class Planner:
    def choose_successor(self, items):
        assert items[0]["step"] == "next"
        return {"id": "next"}


class Queue:
    def __init__(self, jobs=()):
        self.jobs = list(jobs)
        self.replacements = []
        self.submissions = []

    def running(self):
        return list(self.jobs)

    def replace_if_stalled(self, job_id, heartbeat, age):
        self.replacements.append((job_id, heartbeat, age))
        return None

    def submit(self, plan):
        self.submissions.append(plan)
        self.jobs.append(Job(plan["id"], 1000, 1000))
        return True


def test_heartbeat_controls_stall_and_missing_heartbeat_is_not_replayed():
    queue = Queue([Job("alive", 0, 990), Job("stalled", 0, 50),
                   Job("unknown", 0, None)])
    engine = SuccessorEngine(Store(), Planner(), queue, min_inflight=3,
                             stall_seconds=100, clock=lambda: 1000)
    assert engine.successor() == {"submitted": 0, "replaced": 0}
    assert queue.replacements == [("stalled", 50, 100)]


def test_submits_only_to_hosted_floor():
    queue = Queue()
    engine = SuccessorEngine(Store(), Planner(), queue, min_inflight=2,
                             clock=lambda: 1000)
    assert engine.successor() == {"submitted": 2, "replaced": 0}
    assert len(queue.submissions) == 2


def test_bounded_when_hosted_occupancy_does_not_advance():
    queue = Queue()
    queue.submit = lambda plan: True
    engine = SuccessorEngine(Store(), Planner(), queue, min_inflight=10,
                             max_submissions_per_tick=3, clock=lambda: 1000)
    assert engine.successor() == {"submitted": 1, "replaced": 0}

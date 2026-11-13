"""Lanes: the exclusivity guarantee generation relies on, and the width-1 behaviour it must preserve.

Everything here is about one property. A case holds ONE (task-env, artifact) stack for its WHOLE
life, and no other case touches that stack meanwhile. Break either half and the symptom is not an
error -- it is a plausible score computed against a store some other case reset half way through -- so
these are the assertions that have to be mechanical rather than reasoned about.
"""
from __future__ import annotations

import threading
import time

import pytest

from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.dataset import parse_dataset
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery
from appsci_bench.eval.generation import generate
from appsci_bench.eval.lanes import EvalLane, LanePool


def _client(reply="ok"):
    return InProcessArtifactClient(lambda q: ArtifactAgentOutput(text=reply))


def _lane(label="lane-0", *, hooks=False, **kw):
    if hooks:
        kw.setdefault("setup_state", lambda s: None)
        kw.setdefault("read_final_state", lambda s: {})
    return EvalLane(client=_client(), label=label, **kw)


def _dataset(n, *, repeat=1):
    return parse_dataset(
        {"type": "single_turn", "repeat": repeat},
        [{"data_point_id": f"case_{i}", "single_turn_input": {"text": f"q{i}"}} for i in range(n)],
    )


# --- construction -------------------------------------------------------------------------------


def test_a_pool_needs_at_least_one_lane():
    with pytest.raises(ValueError):
        LanePool.exclusive([])


def test_a_shared_pool_refuses_env_hooks():
    """Sharing a lane that resets an environment is the exact mis-scoring lanes exist to prevent:
    several cases would reset and hash one store at the same time."""
    with pytest.raises(ValueError, match="no per-case environment hooks"):
        LanePool.shared(_lane(hooks=True))


def test_a_shared_pool_imposes_no_ceiling():
    """`max_workers is None` means "the caller's own sizing rule still applies" -- which is what keeps
    a task with no per-case environment state running exactly as wide as it always did."""
    pool = LanePool.shared(_lane())
    assert pool.max_workers is None
    assert pool.width == 1
    assert pool.has_env_hooks is False


def test_an_exclusive_pool_ceilings_at_its_width():
    pool = LanePool.exclusive([_lane(f"lane-{i}", hooks=True) for i in range(4)])
    assert pool.max_workers == 4
    assert pool.width == 4
    assert pool.has_env_hooks is True


# --- exclusivity --------------------------------------------------------------------------------


def test_a_pool_hands_out_every_lane_and_then_blocks():
    """Both claims at once, without relying on timing: a 2-lane pool gives out two distinct lanes,
    and a third holder gets nothing until one is returned."""
    pool = LanePool.exclusive([_lane("lane-0", hooks=True), _lane("lane-1", hooks=True)])
    entered = threading.Event()

    with pool.acquire() as first, pool.acquire() as second:
        assert {first.label, second.label} == {"lane-0", "lane-1"}

        def third():
            with pool.acquire():
                entered.set()

        t = threading.Thread(target=third, daemon=True)
        t.start()
        # Both lanes are held here, so the third holder MUST still be waiting. A generous wait keeps
        # this from passing merely because the thread had not been scheduled yet.
        assert not entered.wait(timeout=0.5), "a third case got a lane while both were held"

    # Out of the `with`: both lanes are back, so the waiter proceeds.
    assert entered.wait(timeout=5)
    t.join(timeout=5)


def test_a_raising_holder_still_returns_its_lane():
    """A leaked lane shrinks the pool silently and, once every lane has leaked, hangs the run --
    a deadlock rather than an error, which is the expensive kind to diagnose."""
    pool = LanePool.exclusive([_lane("lane-0", hooks=True)])
    with pytest.raises(RuntimeError):
        with pool.acquire():
            raise RuntimeError("case blew up")
    with pool.acquire() as lane:
        assert lane.label == "lane-0"


def test_a_shared_pool_yields_the_same_lane_to_everyone():
    pool = LanePool.shared(_lane("lane-0"))
    with pool.acquire() as a, pool.acquire() as b:
        assert a is b


# --- what generation does with a pool -------------------------------------------------------------


def test_generation_never_runs_two_cases_on_one_lane():
    """The claim that makes parallel eval safe, asserted mechanically rather than argued.

    Each lane counts its concurrent occupants in its own setup/read hooks; a count above 1 anywhere
    means two cases shared an environment.
    """
    n_lanes, n_cases = 3, 12
    live = [0] * n_lanes
    peak = [0] * n_lanes
    seen: list[list[str]] = [[] for _ in range(n_lanes)]
    guard = threading.Lock()

    def make(i):
        def setup(sample):
            with guard:
                live[i] += 1
                peak[i] = max(peak[i], live[i])
                seen[i].append(sample.data_point_id)
            time.sleep(0.01)

        def read(sample):
            with guard:
                live[i] -= 1
            return {"lane": i}

        return EvalLane(client=_client(), setup_state=setup, read_final_state=read,
                        label=f"lane-{i}")

    pool = LanePool.exclusive([make(i) for i in range(n_lanes)])
    records = generate(_dataset(n_cases), _client(), lanes=pool)

    assert len(records) == n_cases
    assert peak == [1] * n_lanes, f"a lane served two cases at once: {peak}"
    # Every case ran somewhere, exactly once, and the lanes partition them.
    assert sorted(c for lane in seen for c in lane) == sorted(r.data_point_id for r in records)
    assert all(r.final_state is not None for r in records)


def test_generation_spreads_cases_across_lanes():
    """Not just correctness: with 4 lanes and 8 slow cases, more than one lane must actually be used,
    or the pool is serializing and the whole feature is inert."""
    used: set[str] = set()
    guard = threading.Lock()

    def make(i):
        def setup(sample):
            with guard:
                used.add(f"lane-{i}")
            time.sleep(0.05)
        return EvalLane(client=_client(), setup_state=setup, label=f"lane-{i}")

    pool = LanePool.exclusive([make(i) for i in range(4)])
    generate(_dataset(8), _client(), lanes=pool)
    assert len(used) > 1


def test_a_multi_turn_case_stays_on_one_lane_for_every_turn():
    """A lane taken per TURN would strand a multi-turn artifact's own per-conversation state on
    whichever stack answered the first one."""
    # case task -> the set of lanes that answered any of its turns. More than one entry for a case
    # means that conversation moved stacks mid-way.
    lanes_per_case: dict[str, set[str]] = {}
    guard = threading.Lock()

    class Sim:
        """Three turns per conversation, each message naming the case so a turn is attributable."""

        def __init__(self):
            self.tasks: dict[str, str] = {}
            self.counter = 0

        def create_session(self, sample):
            self.counter += 1
            sid = f"sim-{self.counter}"
            self.tasks[sid] = sample.user_task
            return sid

        def ask(self, session_id, conversation):
            if len(conversation) >= 3:
                return None
            return ArtifactAgentQuery(text=f"{self.tasks[session_id]} turn {len(conversation)}")

        def delete_session(self, session_id):
            self.tasks.pop(session_id, None)

    def make(i):
        label = f"lane-{i}"

        def respond(query):
            case = (query.text or "").split(" turn ")[0]
            with guard:
                lanes_per_case.setdefault(case, set()).add(label)
            time.sleep(0.01)
            return ArtifactAgentOutput(text="ok")

        return EvalLane(client=InProcessArtifactClient(respond),
                        setup_state=lambda s: None, label=label)

    dataset = parse_dataset(
        {"type": "multi_turn", "max_turns": 5},
        [{"data_point_id": f"case_{i}",
          "multi_turn_input": {"user_task": f"t{i}", "user_persona": "p"}}
         for i in range(6)],
    )
    pool = LanePool.exclusive([make(i) for i in range(3)])
    generate(dataset, _client(), user_sim=Sim(), lanes=pool)

    assert len(lanes_per_case) == 6
    for case, labels in lanes_per_case.items():
        assert len(labels) == 1, f"{case} was answered by more than one lane: {sorted(labels)}"


# --- width 1 keeps meaning what it meant ----------------------------------------------------------


def test_env_hooks_without_a_pool_still_run_one_case_at_a_time():
    """The behaviour the old `workers = 1` clamp produced, now a consequence of there being one
    environment rather than a special case in the loop."""
    live = 0
    peak = 0
    guard = threading.Lock()

    def setup(sample):
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        time.sleep(0.01)

    def read(sample):
        nonlocal live
        with guard:
            live -= 1
        return {}

    generate(_dataset(6), _client(), setup_state=setup, read_final_state=read)
    assert peak == 1


def test_no_hooks_and_no_pool_still_runs_concurrently():
    """The other half: a task with no per-case environment state must not be slowed down by lanes."""
    live = 0
    peak = 0
    guard = threading.Lock()

    def respond(query):
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with guard:
            live -= 1
        return ArtifactAgentOutput(text="ok")

    generate(_dataset(8), InProcessArtifactClient(respond))
    assert peak > 1


def test_max_workers_is_clamped_to_the_lane_count():
    """An explicit override above the pool's width would leave the surplus threads parked in
    `acquire()` for the whole run, which looks like a hang rather than a misconfiguration."""
    peak = 0
    live = 0
    guard = threading.Lock()

    def make(i):
        def setup(sample):
            nonlocal live, peak
            with guard:
                live += 1
                peak = max(peak, live)
            time.sleep(0.02)

        def read(sample):
            nonlocal live
            with guard:
                live -= 1
            return {}

        return EvalLane(client=_client(), setup_state=setup, read_final_state=read,
                        label=f"lane-{i}")

    pool = LanePool.exclusive([make(i) for i in range(2)])
    generate(_dataset(8), _client(), lanes=pool, max_workers=16)
    assert peak <= 2


# --- which copy served each case, recorded rather than inferable -----------------------------------


def test_each_record_says_which_lane_served_it():
    """The first question asked when a run fails in a way that is not about the artifact: if one
    copy's environment came up bad, ITS cases fail and the others' do not. Without this on the record
    that reads as a scattered failure rate with nothing tying the failures together -- and the
    container logs that would show it are gone by the time anyone looks."""
    pool = LanePool.exclusive([_lane(f"lane-{i}", hooks=True) for i in range(3)])
    records = generate(_dataset(12), _client(), lanes=pool)

    assert all(r.lane is not None for r in records)
    assert {r.lane for r in records} <= {"lane-0", "lane-1", "lane-2"}
    # Every case is attributed to exactly one copy, so the load split is auditable after the run
    # rather than only observable from container logs while it is still up.
    assert sum(1 for r in records if r.lane) == 12


def test_a_single_environment_run_records_no_lane():
    """None means "one environment", which is what every transcript predating parallel eval has --
    a reader must be able to tell that from "lane-0 of several"."""
    assert all(r.lane is None for r in generate(_dataset(3), _client()))
    with_hooks = generate(_dataset(3), _client(), setup_state=lambda s: None)
    assert all(r.lane is None for r in with_hooks)

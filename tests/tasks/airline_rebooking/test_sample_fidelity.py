"""The 50 airline eval samples must carry upstream's ground truth unchanged.

WHY THIS IS NOT A BYTE COMPARISON. `provenance/airline/tasks.json` is a verbatim copy of upstream's
`data/tau2/domains/airline/tasks.json` and can be diffed byte-for-byte, but the eval samples cannot:
`scripts/convert_tau2_airline.py` casts each upstream task into our sample schema, collapsing upstream's
newlines into flowing prose, prefixing labels, and adding two fields of our own (`user_stop_criteria`,
the deliberate fold-down of tau2's three stop tokens, and `eval_rubric`). Byte-identity would fail for
reasons that are correct by design, so it would have to be ignored, which is worse than no check.

What this asserts instead is the property the parity claim actually rests on: every value the conversion
TAKES from upstream arrives unchanged, and nothing upstream defines is silently dropped. That is what
makes `task_success` comparable to a published tau2 number -- the reward is computed from these carried
fields, so a lossy conversion would move scores while every other parity check still passed.

Regenerating the samples from a newer upstream snapshot should keep these green; if one fails, the
conversion changed meaning, not just formatting.
"""
from __future__ import annotations

import json
import re

from appsci_bench.core.spec import TASKS_DIR

_UPSTREAM_TASKS = TASKS_DIR.parent / "provenance" / "airline" / "tasks.json"
_EVAL_DATA = TASKS_DIR / "airline_rebooking" / "eval_data"

#: Upstream's reward inputs. These four are what `task_success` reads, so they are the fields whose
#: fidelity decides whether our number means the same thing as upstream's.
_CRITERIA_FIELDS = ("actions", "communicate_info", "nl_assertions", "reward_basis")


def _upstream() -> list[dict]:
    return json.loads(_UPSTREAM_TASKS.read_text())


def _samples() -> list[dict]:
    paths = sorted(_EVAL_DATA.glob("tau2_airline_*/sample.json"),
                   key=lambda p: int(p.parent.name.rsplit("_", 1)[1]))
    return [json.loads(p.read_text()) for p in paths]


def _leaves(obj, path=""):
    """Flatten to {dotted.path: scalar}.

    Leaf-level so a failure names the one value that moved. Comparing whole nested objects reports the
    entire structure as different and hides which field it was -- a mistake made while first verifying
    this conversion, where a whole-value diff printed entire `dates` dicts and said nothing useful.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


def _norm(text: str | None) -> str:
    """Collapse whitespace: the conversion reflows upstream's newlines, which carries no meaning."""
    return re.sub(r"\s+", " ", text or "").strip()


def test_there_is_one_eval_sample_per_upstream_task():
    upstream, samples = _upstream(), _samples()
    assert len(upstream) == 50
    assert len(samples) == len(upstream)
    assert len({s["data_point_id"] for s in samples}) == len(samples)


def test_every_reward_input_survives_the_conversion_unchanged():
    """The four criteria fields must match leaf-for-leaf; these are what the reward is computed from."""
    upstream, samples = _upstream(), _samples()
    differences: list[str] = []
    for up, sample in zip(upstream, samples):
        ours = json.loads(sample["expected_output"]["json"])
        want = up["evaluation_criteria"]
        for field in _CRITERIA_FIELDS:
            a = dict(_leaves(want[field], field))
            b = dict(_leaves(ours.get(field), field))
            for key in sorted(set(a) | set(b)):
                if a.get(key, "<absent>") != b.get(key, "<absent>"):
                    differences.append(
                        f"{sample['data_point_id']} {key}: "
                        f"upstream={a.get(key, '<absent>')!r} ours={b.get(key, '<absent>')!r}")
    assert not differences, "conversion altered upstream reward inputs:\n" + "\n".join(differences[:20])


def test_every_scenario_clause_upstream_defines_reaches_the_sample():
    """Nothing upstream says to the user may be dropped or reworded.

    `unknown_info` is the one worth guarding: it is populated on only 8 of the 50 tasks, and it is what
    makes those cases require the agent to look something up rather than be told it. Losing it would
    quietly make 8 cases easier while every criteria check still passed.
    """
    missing: list[str] = []
    for up, sample in zip(_upstream(), _samples()):
        instructions = up["user_scenario"]["instructions"]
        turn = sample["multi_turn_input"]
        haystack = " ".join(_norm(v) for v in turn.values() if isinstance(v, str))
        for field in ("reason_for_call", "task_instructions", "known_info", "unknown_info"):
            value = instructions.get(field)
            if value and _norm(value) not in haystack:
                missing.append(f"{sample['data_point_id']}: {field} -> {_norm(value)[:100]!r}")
    assert not missing, "upstream scenario text missing from samples:\n" + "\n".join(missing[:20])


def test_the_user_persona_carries_upstream_known_info():
    """Identity belongs in the persona, not the task: the sim must know who it is before it speaks."""
    for up, sample in zip(_upstream(), _samples()):
        known = up["user_scenario"]["instructions"].get("known_info")
        if known:
            assert _norm(known) in _norm(sample["multi_turn_input"]["user_persona"]), \
                f"{sample['data_point_id']}: known_info not in user_persona"


def test_samples_add_a_stop_criterion_and_nothing_else_of_substance():
    """The stop criterion is ours by design (it folds upstream's three stop tokens into one judged
    criterion, since our sim has no token channel). Asserting it is present on every sample keeps the
    termination gate fair: the gate scores a turn-ceiling hit as 0, which is only just if the sim has a
    way to end the conversation deliberately."""
    for sample in _samples():
        criteria = sample["multi_turn_input"].get("user_stop_criteria")
        assert criteria and criteria.strip(), \
            f"{sample['data_point_id']}: missing user_stop_criteria"

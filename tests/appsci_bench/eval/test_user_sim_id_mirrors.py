"""The eval user-sim model id is duplicated in five places; they must agree.

There is no shared constant to import, because the copies live in two zsh scripts and in a template
that ships without `appsci_bench` importable. So the agreement is asserted here instead.

WHY THIS IS WORTH A TEST. A stale copy does not fail loudly -- it fails in the worst available way.
Per scripts/experiments/refresh_gateway_env.sh, a model id the gateway does not know makes every
multi_turn data point error, an errored result is FORCED to 0.0, and 100-of-100 failures aggregate to a
clean `0.000` with a `done` status and no warning. A full airline run reported exactly that, and it was
indistinguishable in the results table from an agent that built something worthless. The jobs-host copy
already drifted from the laptop copy once, leaving the box unable to run an eval a laptop could.

The same reasoning as the SOP mirror guard in tests/appsci_bench/core/test_agent_spec.py: where a value
has to be duplicated for a structural reason, a test is what keeps the duplicates honest.
"""
from __future__ import annotations

import re

from appsci_bench.core.spec import TASKS_DIR
from appsci_bench.eval.artifact_agent_user_sim import (
    _NEAR_PARITY_ALIASES, TAU_BENCH_USER_SIM_MODEL,
)

_REPO = TASKS_DIR.parent


def _gateway_alias() -> str:
    """The single gateway alias the scripts are expected to name."""
    assert len(_NEAR_PARITY_ALIASES) == 1, (
        "this guard assumes exactly one near-parity alias; extend it if that changes")
    return next(iter(_NEAR_PARITY_ALIASES))


def _defaults_in(rel_path: str) -> list[str]:
    """Every `ARTIFACT_USERSIM_MODEL:-<id>` default written in a shell script."""
    text = (_REPO / rel_path).read_text()
    return re.findall(r"ARTIFACT_USERSIM_MODEL:-([^}\"']+)", text)






def test_run_tasks_warns_about_the_pinned_model():
    """run_tasks.sh only decides which credential to look for, so it names the pin, not the alias."""
    assert _defaults_in("scripts/run_tasks.sh") == [TAU_BENCH_USER_SIM_MODEL]


def test_the_human_baseline_overlay_uses_the_same_gateway_alias():
    """Covered explicitly because nothing else imports this module.

    It ships inside the human-baseline template and runs in a subprocess without `appsci_bench` on the
    path, so it cannot import the constant -- and before this test, a stale copy here would have
    silently scored human baselines against the previous user simulator.
    """
    text = (_REPO / "human_baseline" / "template" / "gateway_env.py").read_text()
    declared = re.findall(r'_NEAR_PARITY_USERSIM_MODEL = "([^"]+)"', text)
    assert declared == [_gateway_alias()], (
        f"human_baseline overlay declares {declared}; expected [{_gateway_alias()!r}]")



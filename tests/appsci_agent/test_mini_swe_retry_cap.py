"""The retry-ladder cap on mini-swe model calls.

Why this is worth a test rather than trusting the line: the cap is applied by setting an environment
variable that mini-swe's own `retry()` helper reads at query time. Nothing fails loudly if the name
drifts or the assignment is removed -- the ladder silently returns to 10 attempts, and the only
symptom is a build that stalls for ~49 minutes on a request the gateway was never going to accept.
"""
from __future__ import annotations

import inspect
import os

import pytest

from appsci_agent.tools import mini_swe_agent as msa

RETRY_ENV = "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"


def test_the_cap_is_small_enough_to_bound_a_doomed_request():
    """Two nested ladders multiply. With litellm's `num_retries=3` (4 attempts) and a gateway that
    hangs up after ~60s of no bytes, each mini-swe attempt costs ~244s -- so the cap is what decides
    whether one unacceptable request costs ~12 minutes or ~49.
    """
    assert 1 <= msa.MINI_SWE_RETRY_ATTEMPTS <= 4, (
        "a larger cap re-creates the stall this bounds; a cap of 0 would disable retrying, which "
        "measurement showed is worth keeping (every order_to_cash crossing was rescued by a retry)"
    )


def test_mini_swe_retry_env_var_matches_the_name_the_library_reads():
    """Pinned against the installed library rather than a literal, so upgrading mini-swe to a version
    that renames or drops this knob fails here instead of silently restoring the 10-attempt ladder.
    """
    from minisweagent.models.utils import retry as retry_mod

    src = inspect.getsource(retry_mod)
    assert RETRY_ENV in src, (
        f"mini-swe no longer reads {RETRY_ENV}; the retry cap in mini_swe_agent.py is now a no-op "
        "and needs reimplementing against whatever replaced it"
    )


def test_the_uncapped_library_default_is_still_the_one_the_arithmetic_assumes():
    """Documents the number the applied comment claims (10), so its arithmetic stays checkable."""
    from minisweagent.models.utils import retry as retry_mod

    src = inspect.getsource(retry_mod).replace("'", '"')
    assert f'os.getenv("{RETRY_ENV}", "10")' in src, (
        "mini-swe's uncapped default is no longer 10; update the comment's arithmetic"
    )


@pytest.mark.parametrize("preset", [None, "7"])
def test_an_operator_export_wins_but_an_unset_value_gets_the_cap(monkeypatch, preset):
    """`setdefault`, not assignment: an operator debugging a flaky gateway may deliberately raise the
    ladder, and this must not silently overwrite that. But the common case -- nothing exported -- must
    end up capped rather than at the library default.
    """
    if preset is None:
        monkeypatch.delenv(RETRY_ENV, raising=False)
    else:
        monkeypatch.setenv(RETRY_ENV, preset)

    # Exercise exactly the assignment the tool performs, rather than standing up a sub-agent run.
    os.environ.setdefault(RETRY_ENV, str(msa.MINI_SWE_RETRY_ATTEMPTS))

    expected = preset if preset is not None else str(msa.MINI_SWE_RETRY_ATTEMPTS)
    assert os.environ[RETRY_ENV] == expected


def test_the_cap_is_actually_applied_when_the_tool_configures_a_model(monkeypatch):
    """The assignment must live on the path that runs, not merely exist in the module.

    Greps the tool's source for the assignment rather than executing a full delegation, because
    standing up a real sub-agent needs Docker, a workspace and a gateway. Coarse, but it fails if the
    line is deleted -- which is the regression that would silently restore the 49-minute stall.
    """
    src = inspect.getsource(msa)
    assert "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT" in src
    assert "MINI_SWE_RETRY_ATTEMPTS" in src
    assert "os.environ.setdefault" in src

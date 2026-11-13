"""The run_tasks config expander (scripts/_expand_runs.py).

The contract under test is the ROW SHAPE, because that is what has broken before: the shell reads
these rows with `IFS=$'\x1f' read -r ...`, so a field that goes missing shifts every later field left
and the orchestrator receives one argument's value in another's flag. Every row therefore carries the
same number of fields whether or not the config set the optional ones.

One row per (task, persona), fields separated by US (0x1f):
    task, persona_file, limit, timeout, observability, agent_model
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "_expand_runs.py"
SEP = "\x1f"
FIELDS = 6


def _run(tmp_path: pathlib.Path, config: str) -> subprocess.CompletedProcess:
    cfg = tmp_path / "runs.yaml"
    cfg.write_text(config, encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), str(cfg)],
                          capture_output=True, text=True, cwd=REPO)


def _expand(tmp_path: pathlib.Path, config: str) -> list[list[str]]:
    """Run the expander on `config` and return its rows, each split into fields."""
    proc = _run(tmp_path, config)
    assert proc.returncode == 0, proc.stderr
    return [line.split(SEP) for line in proc.stdout.splitlines() if line]


def test_row_has_every_field_even_when_optionals_are_omitted(tmp_path):
    rows = _expand(tmp_path, """
runs:
  - task: airline_rebooking
    personas: l3
""")
    assert len(rows) == 1
    assert len(rows[0]) == FIELDS, rows[0]
    task, persona, limit, timeout, obs, model = rows[0]
    assert task == "airline_rebooking"
    assert persona.endswith("l3.yaml")
    assert limit == ""          # "all data points"
    assert timeout == "10800"   # the built-in default (raised from 7200 on 2026-09-16)
    assert obs == "true"
    assert model == ""          # no --agent-model is passed at all


def test_top_level_agent_model_applies_to_every_row(tmp_path):
    rows = _expand(tmp_path, """
agent_model: fireworks_ai/accounts/fireworks/models/kimi-k3
runs:
  - task: airline_rebooking
    personas: [l1, l3]
""")
    assert len(rows) == 2
    assert {r[-1] for r in rows} == {"fireworks_ai/accounts/fireworks/models/kimi-k3"}


def test_per_run_agent_model_overrides_the_top_level_one(tmp_path):
    rows = _expand(tmp_path, """
agent_model: claude-opus-4-8
runs:
  - task: airline_rebooking
    personas: l3
  - task: order_to_cash
    personas: l3
    agent_model: fireworks_ai/kimi-k3
""")
    by_task = {r[0]: r[-1] for r in rows}
    assert by_task == {"airline_rebooking": "claude-opus-4-8",
                       "order_to_cash": "fireworks_ai/kimi-k3"}


def test_empty_limit_still_keeps_later_fields_in_place(tmp_path):
    """The reason the delimiter is US and not a tab: an empty `limit` must hold its position, or the
    shell reads the timeout as the limit and `true` as the timeout."""
    rows = _expand(tmp_path, """
agent_model: fireworks_ai/kimi-k3
observability: false
runs:
  - task: airline_rebooking
    personas: l3
    timeout: 60
""")
    _, _, limit, timeout, obs, model = rows[0]
    assert (limit, timeout, obs, model) == ("", "60", "false", "fireworks_ai/kimi-k3")


def test_agent_model_carrying_a_newline_is_rejected(tmp_path):
    """A newline inside the value would emit a second, malformed line, and the shell would read it as
    another run. Rejected here instead.

    A raw US byte is caught earlier and elsewhere -- YAML's own reader refuses control characters -- so
    the escaped forms are what a config can actually reach.
    """
    proc = _run(tmp_path, """
runs:
  - task: airline_rebooking
    personas: l3
    agent_model: "two\\nlines"
""")
    assert proc.returncode != 0
    assert "agent_model" in proc.stderr


def test_agent_model_carrying_a_carriage_return_is_rejected(tmp_path):
    """\\r is the one that would otherwise slip through: the run-id builder sanitises it, litellm does
    not, so it would reach the provider inside the model id."""
    proc = _run(tmp_path, """
runs:
  - task: airline_rebooking
    personas: l3
    agent_model: "kimi\\rk3"
""")
    assert proc.returncode != 0
    assert "agent_model" in proc.stderr


def test_a_non_string_agent_model_is_rejected(tmp_path):
    """YAML coerces before the expander sees it: unquoted `yes` arrives as a bool and would be sent as
    the model id "True", which reaches the provider as a real (and wrong) id."""
    proc = _run(tmp_path, """
runs:
  - task: airline_rebooking
    personas: l3
    agent_model: yes
""")
    assert proc.returncode != 0
    assert "agent_model" in proc.stderr and "bool" in proc.stderr


def test_a_quoted_yaml_bool_is_a_valid_model_id(tmp_path):
    """The rejection is about the TYPE, not the spelling -- a quoted value is a string and passes."""
    rows = _expand(tmp_path, """
runs:
  - task: airline_rebooking
    personas: l3
    agent_model: "yes"
""")
    assert rows[0][-1] == "yes"


def test_per_run_null_agent_model_falls_back_to_the_default(tmp_path):
    """Same shape as `limit: null`: an explicit null on one run puts that run back on the orchestrator
    default even when the top level names a model."""
    rows = _expand(tmp_path, """
agent_model: fireworks_ai/kimi-k3
runs:
  - task: airline_rebooking
    personas: l3
    agent_model: null
  - task: order_to_cash
    personas: l3
""")
    by_task = {r[0]: r[-1] for r in rows}
    assert by_task == {"airline_rebooking": "", "order_to_cash": "fireworks_ai/kimi-k3"}

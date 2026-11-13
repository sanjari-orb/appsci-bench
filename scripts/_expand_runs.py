"""Expand a run_tasks YAML config into one row per (task, persona) run.

Usage:  python scripts/_expand_runs.py <config.yaml>
Output (stdout), one line per run, fields separated by US (0x1f, the ASCII unit separator):
             task<US>persona_file<US>limit<US>timeout<US>observability<US>agent_model
         limit is empty for "all data points"; observability is "true"/"false"; agent_model is
         empty for "whatever the orchestrator defaults to".
Errors go to stderr with a non-zero exit. Consumed by scripts/run_tasks.sh.

NOT tab-separated, deliberately: tab is an IFS *whitespace* character, so bash's `read` collapses
runs of consecutive tabs into one delimiter. An empty `limit` field ("all data points") would then
vanish and shift every later field left -- the shell saw limit=<timeout>, timeout="true". US is
non-whitespace, so `read` preserves empty fields positionally. Keep the two sides in sync.

Config schema (see scripts/run_tasks_yamls/run_tasks.sample.yaml):
    timeout: 10800           # global default (seconds); per-run `timeout:` overrides it
    observability: true      # global; stream the live dashboard
    agent_model: <id>        # global default for the agent under test; per-run `agent_model:`
                             #  overrides it. Omitted => the orchestrator's own default.
    runs:
      - task: airline_rebooking
        personas: all         # "all" = every user_personas/*.yaml; or a list [l1, l2];
                              #  or one name/path. A bare name -> user_personas/<name>.yaml.
        limit: null           # null/omitted = all eval data points; an int caps them
        timeout: 10800        # optional per-run override
        agent_model: <id>     # optional per-run override
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 10800
SEP = "\x1f"  # ASCII unit separator; see the module docstring for why not a tab


def _die(msg: str) -> None:
    print(f"run_tasks config error: {msg}", file=sys.stderr)
    sys.exit(1)


def _persona_files(task: str, personas) -> list[Path]:
    """Resolve a run's `personas` value to concrete persona YAML paths."""
    persona_dir = REPO_ROOT / "tasks" / task / "user_personas"
    if personas is None:
        personas = "l3"  # default when omitted
    if isinstance(personas, str) and personas.strip().lower() == "all":
        found = sorted(persona_dir.glob("*.yaml"))
        if not found:
            _die(f"no persona files under {persona_dir}")
        return found
    items = personas if isinstance(personas, list) else [personas]
    out: list[Path] = []
    for item in items:
        s = str(item).strip()
        if s.endswith(".yaml") or "/" in s:
            p = (REPO_ROOT / s) if not Path(s).is_absolute() else Path(s)
        else:
            p = persona_dir / f"{s}.yaml"
        if not p.is_file():
            _die(f"persona file not found for task '{task}': {p}")
        out.append(p)
    return out


def main() -> None:
    if len(sys.argv) != 2:
        _die("usage: _expand_runs.py <config.yaml>")
    cfg_path = Path(sys.argv[1])
    if not cfg_path.is_file():
        _die(f"config not found: {cfg_path}")
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(cfg, dict):
        _die("top-level YAML must be a mapping")

    global_timeout = cfg.get("timeout", DEFAULT_TIMEOUT)
    global_obs = bool(cfg.get("observability", True))
    global_model = cfg.get("agent_model")
    runs = cfg.get("runs")
    if not isinstance(runs, list) or not runs:
        _die("`runs:` must be a non-empty list")

    rows: list[str] = []
    for i, run in enumerate(runs):
        if not isinstance(run, dict):
            _die(f"runs[{i}] must be a mapping")
        task = run.get("task")
        if not task:
            _die(f"runs[{i}] missing `task`")
        if not (REPO_ROOT / "tasks" / task / "spec.yaml").is_file():
            _die(f"unknown task '{task}' (no tasks/{task}/spec.yaml)")
        timeout = run.get("timeout", global_timeout)
        limit = run.get("limit", None)
        limit_str = "" if limit in (None, "", "all") else str(int(limit))
        # An EMPTY field means "don't pass --agent-model at all", which is not the same as naming the
        # default: the orchestrator builds the run id and the run record from the model it was given,
        # so an id invented here would appear in both for runs that never asked for one.
        model = run.get("agent_model", global_model)
        if model is not None and not isinstance(model, str):
            # YAML coerces before we ever see it: `agent_model: yes` arrives as True and would be sent
            # as the model id "True". A model id is a string; anything else is a typo worth naming.
            _die(f"runs[{i}] `agent_model` must be a string, got {type(model).__name__} "
                 f"({model!r}) -- quote it if the id looks like a YAML bool or number")
        model_str = "" if model is None else model.strip()
        # Any control character is rejected, not just the two that break this format outright: a
        # newline emits a second, malformed line the shell reads as another run, and a US byte splits
        # one field into two and shifts every later field left. A stray \r survives the run-id
        # sanitiser but not litellm. (YAML's own reader refuses most of these first, so in practice
        # this catches the escaped forms.)
        bad = next((c for c in model_str if ord(c) < 0x20 or ord(c) == 0x7f), None)
        if bad is not None:
            _die(f"runs[{i}] `agent_model` may not contain control characters "
                 f"(found {bad!r} in {model_str!r})")
        for pf in _persona_files(task, run.get("personas")):
            rel = pf.relative_to(REPO_ROOT)
            rows.append(SEP.join([task, str(rel), limit_str, str(int(timeout)),
                                  str(global_obs).lower(), model_str]))

    sys.stdout.write("\n".join(rows) + ("\n" if rows else ""))


if __name__ == "__main__":
    main()

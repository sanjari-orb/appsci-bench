"""Automated end-state validation harness.

The artifact cannot read the scoring seams (state_hash / action_trace -- R-20, EXPLORATION.md 2.3),
so this harness validates the way the bank's reviewer conceptually does (V-1/V-2): it runs a
record-grounded scenario through the real Agent and captures the sequence of tool calls the agent
made, then asserts the *mutating* calls (verification + specialist actions + handoffs + escalation)
against the scenario's expected category signature. It never names a specialist action tool (P3)
and never reads a DB seam (R-20) -- every assertion is on the agent's OWN recorded calls.

Per V-3/V-4, verification field VALUES are read from the customer's live record at run time -- never
fabricated. The harness supplies them in the opening user turn so the default flow can verify.

Usage:
    python -m validation.harness                            # all scenarios, write validation/REPORT.md
    python -m validation.harness --scenario KEY             # run one scenario
    python -m validation.harness --scenario KEY --repeat 7  # measure a scenario's k/N reliability

Requires the model + tool env vars (see agent.config). If they are absent it reports that clearly
rather than pretending to pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.config import Config
from agent.loop import Agent
from agent.tool_client import BankingToolClient
from .scenarios import SCENARIOS, Scenario

_HERE = Path(__file__).parent
_REPORT = _HERE / "REPORT.md"


def _parse_record(result: Any) -> dict[str, str]:
    """Pull `field: value` pairs out of a tool result.

    The tool API returns records as a human-readable string, e.g.::

        Found 1 record(s) in 'users':

        1. Record ID: 123
           address: 742 Maple Street, Austin, TX 78701
           date_of_birth: 03/15/1992
           ...

    The model reads this prose directly, but the harness needs the fields programmatically to
    source real verification values (V-4). We parse the first record's indented `key: value` lines.
    """
    if isinstance(result, dict):
        return {str(k): str(v) for k, v in result.items()}
    if not isinstance(result, str):
        return {}
    fields: dict[str, str] = {}
    for line in result.splitlines():
        stripped = line.strip()
        # Only the indented "key: value" body lines; skip headers like "Found 1 record(s)".
        if line.startswith(" ") and ": " in stripped and not stripped[0].isdigit():
            key, _, value = stripped.partition(": ")
            key = key.strip()
            if key and key not in fields:
                fields[key] = value.strip()
    return fields

# Tools whose calls change records (or hand a mutating action to the customer). These are what a
# reviewer compares against the required actions. Reads are excluded.
_MUTATING = {
    "log_verification",
    "change_user_email",
    "call_discoverable_agent_tool",
    "give_discoverable_user_tool",
    "unlock_discoverable_agent_tool",
    "transfer_to_human_agents",
}

# Categories the expected-signature check counts, mapped to the tool names that realise them.
# call_discoverable_agent_tool == a specialist action; give_discoverable_user_tool == a handoff.
# unlock is plumbing, not an action, so it is excluded from the specialist-action count.


class RecordingToolClient(BankingToolClient):
    """Wraps the real client to record every call, so the harness can report the end state."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((name, arguments or {}))
        return super().call(name, arguments)

    def mutating_calls(self) -> list[tuple[str, dict[str, Any]]]:
        return [(n, a) for (n, a) in self.calls if n in _MUTATING]


@dataclass
class Result:
    scenario: Scenario
    ok: bool
    detail: str
    mutating: list[tuple[str, dict[str, Any]]]


def _check_signature(
    scenario: Scenario, muts: list[tuple[str, dict[str, Any]]]
) -> tuple[bool, str]:
    """Assert the recorded mutating calls against the scenario's expected signature (R-20-safe).

    Returns (ok, human-readable detail). All checks are on the agent's OWN calls -- never a DB
    read-back. Categories only (counts, escalation, forbidden-ness); no specialist name is asserted,
    so this stays P3-safe.
    """
    exp = scenario.expected
    names = [n for (n, _) in muts]
    n_verif = names.count("log_verification")
    n_calls = names.count("call_discoverable_agent_tool")
    n_hand = names.count("give_discoverable_user_tool")
    n_esc = names.count("transfer_to_human_agents")

    problems: list[str] = []
    if exp.verifications is not None and n_verif != exp.verifications:
        problems.append(f"verifications={n_verif}, expected {exp.verifications} (R-6/R-7)")
    if exp.forbid_specialist and (n_calls or n_hand):
        problems.append(
            f"forbidden specialist mutation occurred: {n_calls} call(s), {n_hand} handoff(s) "
            "(R-16/R-18)"
        )
    if exp.specialist_calls is not None and n_calls != exp.specialist_calls:
        problems.append(f"specialist_calls={n_calls}, expected {exp.specialist_calls}")
    if exp.handoffs is not None and n_hand != exp.handoffs:
        problems.append(f"handoffs={n_hand}, expected {exp.handoffs} (R-11/R-14)")
    if exp.expect_escalation and n_esc == 0:
        problems.append("expected an escalation (transfer_to_human_agents) but none occurred (R-15)")
    if not exp.expect_escalation and n_esc:
        problems.append(f"unexpected escalation ({n_esc}x) -- KB may cover this request (R-16)")

    summary = (
        f"verif={n_verif} calls={n_calls} handoffs={n_hand} escalate={n_esc}; "
        f"total mutating={len(muts)}"
    )
    if problems:
        return False, summary + " | FAIL: " + "; ".join(problems)
    return True, summary + " | matches expected signature"


def _verification_opening(record: dict[str, Any], fields: tuple[str, ...]) -> str:
    """Build the customer's opening line, supplying two real verification values from the record."""
    parts = []
    for f in fields:
        val = record.get(f)
        if val:
            parts.append(f"my {f.replace('_', ' ')} is {val}")
    creds = "; ".join(parts) if parts else "(no verification fields available)"
    return creds


def run_scenario(scenario: Scenario, config: Config) -> Result:
    tools = RecordingToolClient(config)
    agent = Agent(config)
    # Inject the recording client so we capture what the agent does.
    agent._tools = tools  # noqa: SLF001 -- test harness intentionally swaps the client

    # V-3: read the live record first to source real verification values (never fabricate).
    lookup = tools.call("get_user_information_by_id", {"user_id": scenario.user_id})
    record = _parse_record(lookup.get("result") if isinstance(lookup, dict) else None)
    if "name" not in record:
        return Result(scenario, False,
                      f"could not read live record for {scenario.user_id}: {lookup}", [])
    tools.calls.clear()  # the setup read is ours, not the agent's -- don't count it

    creds = _verification_opening(record, scenario.verify_fields)
    opening = (
        f"Hi, this is {record.get('name')} (user id {scenario.user_id}). "
        f"{scenario.request} For verification, {creds}."
    )
    # Build the running conversation; replay each follow-up as a further user turn so multi-turn
    # behaviour (R-7 log-once, handoff completion, withdrawn requests) is exercised.
    convo: list[dict[str, Any]] = [{"role": "user", "content": opening}]
    try:
        for turn_idx in range(1 + len(scenario.follow_ups)):
            out = agent.run_turn(convo)
            reply = out["choices"][0]["message"]["content"]
            convo.append({"role": "assistant", "content": reply})
            if turn_idx < len(scenario.follow_ups):
                convo.append({"role": "user", "content": scenario.follow_ups[turn_idx]})
    except Exception as exc:  # noqa: BLE001 -- report any failure rather than crash the suite
        return Result(scenario, False, f"run_turn raised: {exc}", tools.mutating_calls())

    muts = tools.mutating_calls()
    ok, detail = _check_signature(scenario, muts)
    return Result(scenario, ok, detail, muts)


def _fmt_calls(calls: list[tuple[str, dict[str, Any]]]) -> str:
    if not calls:
        return "    (none)"
    lines = []
    for name, args in calls:
        shown = {k: v for k, v in args.items() if k != "arguments"} or args
        lines.append(f"    - `{name}`  {shown}")
    return "\n".join(lines)


def write_report(results: list[Result], preamble: str = "") -> None:
    lines = ["# Phase 2 Validation Report", ""]
    if preamble:
        lines += [preamble, ""]
    passed = sum(1 for r in results if r.ok)
    lines += [f"**Automated: {passed}/{len(results)} scenarios matched their expected signature.**",
              "",
              "> Note (V-1/V-2): each check asserts the agent's OWN recorded mutating calls against",
              "> a category signature -- verification count (R-6/R-7), specialist-action and handoff",
              "> counts, and escalation (R-15). It never names a specialist action tool (P3) and",
              "> never reads a DB seam (R-20).", "",
              "| Scenario | Customer | Auto check | Detail |", "|---|---|---|---|"]
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        lines.append(f"| {r.scenario.key} | {r.scenario.customer} | {mark} | {r.detail} |")
    lines += ["", "## Recorded mutating calls per scenario", ""]
    for r in results:
        lines += [f"### {r.scenario.key} ({r.scenario.customer})",
                  f"Request: {r.scenario.request}",
                  f"Expected: {r.scenario.expected_effect}",
                  "", "Mutating calls the agent made:", _fmt_calls(r.mutating), ""]
    # Append the manual integration template so the report is one place for all results.
    template = (_HERE / "MANUAL_TEMPLATE.md")
    if template.exists():
        lines += ["---", "", template.read_text()]
    _REPORT.write_text("\n".join(lines))


def _run_repeat(key: str, n: int, config: Config) -> None:
    """Replay a single scenario N times and print the k/N reliability -- for non-deterministic
    cases like DEF-1 (Bei). Uses the same signature check as the full suite.
    """
    matches = [s for s in SCENARIOS if s.key == key]
    if not matches:
        print(f"No scenario with key {key!r}. Known: {[s.key for s in SCENARIOS]}")
        return
    scenario = matches[0]
    ok_count = 0
    for i in range(1, n + 1):
        result = run_scenario(scenario, config)
        ok_count += int(result.ok)
        print(f"  run {i}/{n}: {'PASS' if result.ok else 'FAIL'} -- {result.detail}")
    print(f"{key}: {ok_count}/{n} PASS (reliability {ok_count / n:.0%})")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Phase 2 end-state validation harness.")
    parser.add_argument("--scenario", help="run only this scenario key")
    parser.add_argument("--repeat", type=int, default=1,
                        help="replay the selected scenario N times to measure reliability")
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except RuntimeError as exc:
        write_report([], preamble=f"**Could not run: {exc}.** Set the model/tool env vars and retry.")
        print(f"Env not configured: {exc}. Wrote REPORT.md noting this.")
        return

    if args.repeat > 1 or args.scenario:
        key = args.scenario or SCENARIOS[0].key
        _run_repeat(key, max(args.repeat, 1), config)
        return

    results = [run_scenario(s, config) for s in SCENARIOS]
    write_report(results)
    passed = sum(1 for r in results if r.ok)
    print(f"{passed}/{len(results)} scenarios matched their signature. Report: {_REPORT}")


if __name__ == "__main__":
    main()

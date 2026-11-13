#!/usr/bin/env python3
"""Cast tau2-bench *banking_knowledge* tasks into appsci-bench eval samples.

WHY
  The banking task (tasks/banking_support/spec.yaml) is an open-source task derived from the
  tau2-bench banking_knowledge domain (https://github.com/sierra-research/tau2-bench). Unlike a
  pure-QA task, banking_knowledge is a KNOWLEDGE-GROUNDED *TRANSACTIONAL* customer-service domain
  ("Rho-Bank"): the agent must retrieve rules/product specs from a large knowledge base, verify the
  customer, and drive the CORRECT TRANSACTIONAL OUTCOME. Each upstream task carries a GOLDEN ground
  truth: the tool calls that define the correct end state (`actions`) + `reward_basis`. tau2 scores a
  run as the product of its `reward_basis` components -- for banking that is one of:
    * DB     (88/97 tasks) -- apply the golden `actions` to a fresh DB (seeded with the case's
                              `initial_state`); the agent's final DB must hash-match.
    * ACTION (9/97 tasks)  -- the agent's action trajectory must match the golden `actions`.
  `communicate_info` is EMPTY for all 97 tasks, so tau2's COMMUNICATE component is vacuous here.

  This converter is the OFFLINE step the spec's `source` block records. It reads the vendored
  upstream tasks (provenance/banking_support/tasks.json) and emits eval samples
  (schema/dataset.schema.json $defs/eval_sample), carrying the tau2 ground truth VERBATIM into each
  sample's `expected_output.json` so the eval functions (tasks/banking_support/eval.py, added in a
  later phase) can replay tau2's exact DB/ACTION scoring. `eval_rubric` carries NL guidance for the
  secondary LLM judges (groundedness / verification / deflection) to read.

FIELD MAPPING (tau2 task -> eval_sample)   [see tasks/banking_support/seed_data/PROVENANCE.md]
  id ("task_001")                                 -> data_point_id ("tau2_banking_<n>", numeric)
  user_scenario.instructions (a single free-text  -> multi_turn_input.user_task (verbatim; the
    role-play string; NOT a dict as in airline)       customer's goal + behavior. Does NOT name the
                                                       golden action, so non-leaking by construction)
  (fixed pointer string)                          -> multi_turn_input.user_persona (banking has no
                                                       separate persona; user_scenario.persona=null)
  (fixed, one value for all 97)                   -> multi_turn_input.user_stop_criteria (upstream's
                                                       three end-of-conversation conditions; per-case
                                                       endings ride in user_task, as upstream has them)
  evaluation_criteria.{actions, communicate_info,
    reward_basis} + required_documents +
    user_tools + initial_state + purpose          -> expected_output.json (full struct, JSON-encoded;
                                                       the sample schema is additionalProperties:false
                                                       so ALL per-sample data rides here)
  (composed guidance)                             -> eval_rubric (for the secondary judges)

WHY initial_state rides in expected_output.json
  75/97 tasks seed the DB per case via `initial_state`. The eval_sample schema has no top-level
  slot for it (additionalProperties:false), so it is carried inside expected_output.json; the
  per-sample env-reset hook (reset_env_per_sample, wired in a later phase) reads it from there and
  applies it to a fresh DB before the golden actions are replayed.

This is a faithful, deterministic 1:1 cast -- no sampling, no synthesis. Re-running on the same
vendored tasks.json yields byte-identical output (samples sorted by numeric upstream id).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASK_DIR = REPO / "tasks" / "banking_support"
DEFAULT_SRC = REPO / "provenance" / "banking_support" / "tasks.json"
DEFAULT_OUT = TASK_DIR / "eval_data"

# WHO is calling: banking's upstream user_scenario.persona is null for every task and there is no
# separate known_info field (airline had one). The customer's identity + goal + behavior all live in
# the single free-text `instructions` string, which becomes user_task. So user_persona is a fixed,
# non-leaking pointer that mirrors tau2's standing behavioral contract; the artifact user sim renders
# BOTH user_task and user_persona, so the full instructions still reach it.
USER_PERSONA = (
    "You are a Rho-Bank customer contacting customer service. Stay in character per your task "
    "instructions: reveal details only when the agent asks, never act as the agent, and pursue "
    "your goal as described."
)

# WHEN to stop talking. One string for all 97 cases, mirroring how upstream is actually built: every
# domain shares ONE global set of stop conditions, and all per-case variation lives inside the
# role-play instructions -- which build_user_task already carries verbatim. So this is the global half
# of that pair, not a generic stand-in for detail we dropped. Authoring 97 bespoke criteria would
# instead MOVE termination logic out of the instruction text into a field upstream has no analog for,
# and a re-conversion could no longer regenerate it.
#
# The three conditions are upstream's three end-of-conversation markers, restated for a sim that
# signals through a structured field instead of emitting a token:
#   (a) goal satisfied            (b) transferred to a human       (c) scenario exhausted
#
# DELIBERATELY NOT airline's extra "the agent has clearly refused and you have no further argument"
# clause: upstream's goal-satisfied condition has no such exit, and refuse-then-escalate is banking's
# single most common shape (27 cases script a transfer demand). Importing that clause would license
# stopping early in exactly the cases most sensitive to it.
#
# THE TOKEN CLAUSE IS THE LOAD-BEARING PART. 43 of the 97 instruction strings script their own ending
# with a literal marker (typically a closing line followed by `###STOP###`) and one forbids a
# particular marker. All of that text already reaches the sim inside user_task -- but a sim that
# signals via a boolean has no reason to read a bare marker as an instruction, so without this clause
# it is inert noise mid-prompt. Making instructions authoritative is also what handles the case that
# says NOT to end a given way.
#
# The last sentence guards a trap specific to this domain: 19 cases hand the CUSTOMER a transfer-request
# tool, and in only 3 is using it the correct outcome. Treating the customer's own escalation as an
# ending would corrupt all 19 -- the other 16 would gain a spurious DB record and fail, and the 3 would
# short-circuit the conversation the request is supposed to conclude.
USER_STOP_CRITERIA = (
    "Stop when any one of these is true: (a) the goal in your task instructions has been met; "
    "(b) the agent has clearly told you it is transferring you to a human agent -- stop once it has "
    "actually said so, not merely offered to; or (c) continuing would need information your "
    "instructions do not give you. Your task instructions override all three: if they script how or "
    "when the conversation ends -- a closing line you are told to say, or an end-of-conversation "
    "marker such as ###STOP### or ###OUT-OF-SCOPE### -- follow them, and if they tell you not to end "
    "a particular way, do not. Treat such a marker as an instruction addressed to you, never as words "
    "to say: deliver the closing line if you have one, then stop, and never type the marker itself. "
    "Requesting a human transfer YOURSELF is an action you take, not a reason to stop -- only the "
    "agent telling you it is transferring you ends the conversation."
)

# tau2 read-only tools -- listed so the console summary can flag cases whose oracle contains no
# state-mutating (write) action (informational only; does not affect output).
_READ_TOOLS = {
    "get_current_time", "get_user_information_by_id", "get_user_information_by_name",
    "get_user_information_by_email", "get_referrals_by_user",
    "get_credit_card_transactions_by_user", "get_credit_card_accounts_by_user",
    "list_discoverable_agent_tools",
}


def _clean(text: str | None) -> str:
    """Strip outer whitespace, normalize CRLF -> LF, and drop trailing whitespace on each line,
    preserving the multi-paragraph structure of the role-play instructions (unlike airline's
    single-paragraph collapse -- banking instructions are longer and structured)."""
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def data_point_id(task: dict) -> str:
    """'task_001' -> 'tau2_banking_1'. Numeric, matching the airline convention."""
    raw = task["id"]
    num = raw[len("task_"):] if raw.startswith("task_") else raw
    return f"tau2_banking_{int(num)}" if num.isdigit() else f"tau2_banking_{num}"


def _sort_key(task: dict):
    raw = task["id"]
    num = raw[len("task_"):] if raw.startswith("task_") else raw
    return (0, int(num)) if num.isdigit() else (1, raw)


#: Where the customer-held tools' signatures come from. Same reference the stakeholder simulator is
#: given, read here at CONVERSION time so the signature is baked into each sample rather than resolved
#: per run. A name the reference does not cover is emitted as a bare name and reported, never guessed.
TOOLS_REFERENCE = REPO / "tasks" / "banking_support" / "seed_data" / "tools_reference.json"


def user_tools_with_signatures(names: list[str]) -> list[dict]:
    """Each granted customer tool as `{name, description?, parameters?}` for the sample.

    The sim can only fill a tool's arguments from the signature the case carries -- its instructions
    forbid inventing values -- so converting a name to a bare `{"name": ...}` leaves that tool callable
    only if it takes no arguments. Emitting the signature here is what makes the 49 customer-action
    cases reachable. Kept in the sample rather than resolved from a task-level pointer at run time so
    every per-case fact about the simulator lives in one place; `task_runner` starts the build phase's
    environment too, and that phase has no customer simulator at all.
    """
    if not names:
        return []
    reference = json.loads(TOOLS_REFERENCE.read_text())
    signatures = {
        (entry.get("function") or {}).get("name"): (entry.get("function") or {})
        for entry in reference
        if entry.get("role") == "user" and (entry.get("function") or {}).get("name")
    }
    out: list[dict] = []
    for name in names:
        fn = signatures.get(name)
        if fn is None:
            print(f"  WARNING: no signature for customer tool {name!r} in {TOOLS_REFERENCE.name} -- "
                  "emitting a bare name; the sim will not be able to pass it arguments")
            out.append({"name": name})
            continue
        item: dict = {"name": name}
        description = (fn.get("description") or "").strip()
        if description:
            item["description"] = description
        parameters = fn.get("parameters") or {}
        if parameters.get("properties"):
            item["parameters"] = parameters
        out.append(item)
    return out


def build_user_task(task: dict) -> str:
    """WHAT the caller wants + how they behave -- the upstream `instructions` string verbatim
    (cleaned). This is the customer's role-play script and is exactly what drives the artifact user
    sim. It deliberately does NOT include the golden actions, required_documents, or initial_state
    (see to_sample) -- so no oracle leakage."""
    return _clean(task["user_scenario"]["instructions"])


def build_eval_rubric(task: dict) -> str:
    """NL guidance for the secondary judges (groundedness / verification / deflection). banking has
    no upstream nl_assertions, so this is composed from the reward basis + the domain policy's
    grounding/verification expectations. Structured params (actions, required_documents, ...) go in
    expected_output.json, NOT here."""
    ec = task["evaluation_criteria"]
    basis = ec.get("reward_basis") or []
    purpose = _clean((task.get("description") or {}).get("purpose"))

    lines = []
    # `purpose` is often just "Task: task_NNN" (uninformative); include only when it says more.
    if purpose and purpose.lower() != f"task: {task['id']}".lower():
        lines.append(f"Purpose: {purpose}")

    if "DB" in basis:
        scoring = (
            "Scored on tau2 reward_basis ['DB']: the agent succeeds when the final database state "
            "matches applying the oracle actions to a fresh database seeded with this case's "
            "initial_state."
        )
    elif "ACTION" in basis:
        scoring = (
            "Scored on tau2 reward_basis ['ACTION']: the agent succeeds when its action trajectory "
            "matches the oracle actions."
        )
    else:
        scoring = f"Scored on tau2 reward_basis {basis}."
    lines.append(scoring)

    lines.append(
        "Correct handling requires retrieving the relevant policy/product documents from the "
        "knowledge base (the oracle's required_documents lists the docs the correct answer depends "
        "on) and grounding all advice in them without fabrication; verifying the customer's identity "
        "before taking account actions; and transferring to a human only when the knowledge base "
        "describes no applicable action."
    )
    return " ".join(lines)


def to_sample(task: dict) -> dict:
    """One EvalSample dict (schema/dataset.schema.json $defs/eval_sample) carrying the full tau2
    ground truth. Drives the artifact agent user sim rather than a single query."""
    ec = task["evaluation_criteria"]

    expected = {
        # The authoritative ground truth: applying these write actions to a fresh (seeded) DB
        # defines the expected final DB state (tau2 'DB' check); for 'ACTION' tasks the trajectory
        # itself is compared. Read actions are listed too for traceability.
        "actions": ec.get("actions") or [],
        # Substrings that must appear in an agent message (tau2 'COMMUNICATE' check). Empty for all
        # banking tasks -- carried for parity / forward-compat.
        "communicate_info": ec.get("communicate_info") or [],
        # Which components multiply into the tau2 reward (banking = DB or ACTION).
        "reward_basis": ec.get("reward_basis") or [],
        # The KB documents the correct handling depends on -- the ground-truth grounding set, for
        # the groundedness judge and an optional retrieval-recall diagnostic.
        "required_documents": task.get("required_documents") or [],
        # The customer-held write tools for this case (the user, not the agent, executes these; the
        # eval-time user sim must be able to call them -- see the plan's P5 note).
        "user_tools": task.get("user_tools") or [],
        # Per-sample DB seed (null for 22 tasks). The reset_env_per_sample hook applies this to a
        # fresh DB before replaying the golden actions. Lives here because the sample schema is
        # additionalProperties:false (no top-level slot for it).
        "initial_state": task.get("initial_state"),
        # Author's note on what the case is probing, kept for auditability.
        "purpose": _clean((task.get("description") or {}).get("purpose")),
    }

    return {
        "data_point_id": data_point_id(task),
        "multi_turn_input": {
            "user_task": build_user_task(task),
            "user_persona": USER_PERSONA,
            # WHEN the sim ends the conversation. Absent, every case silently inherited the framework
            # default ("The user task has been successfully completed"), which covers only the first of
            # upstream's three stop conditions -- leaving transfer and scenario-exhausted with no
            # representation at all, in a domain where 27 cases script a transfer demand.
            "user_stop_criteria": USER_STOP_CRITERIA,
            # The customer's OWN tools, mirrored out of the oracle into the sim's input, each carrying
            # its SIGNATURE. Non-leaking: these are capabilities the customer would obviously know they
            # have (upstream gives them to its user simulator too), and neither a name nor an argument
            # list says which action is correct. The sim needs them here because 49 cases have a golden
            # action the CUSTOMER performs -- with a sim that can only talk, no artifact could reach the
            # golden state on those -- and it needs the ARGUMENTS because its instructions forbid
            # inventing values, so a bare name is callable only for a tool that takes none.
            "user_tools": user_tools_with_signatures(task.get("user_tools") or []),
        },
        "expected_output": {"json": json.dumps(expected)},
        "eval_rubric": build_eval_rubric(task),
    }


def write_samples(samples: list[dict], out: Path) -> None:
    """One subdirectory per data_point_id, each holding its sample.json (dataset.schema.json's
    per-sample-directory convention -- see appsci_bench/eval/dataset.py's loader)."""
    out.mkdir(parents=True, exist_ok=True)
    for s in samples:
        sample_dir = out / s["data_point_id"]
        sample_dir.mkdir(parents=True, exist_ok=True)
        (sample_dir / "sample.json").write_text(json.dumps(s, indent=2) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC,
                    help="vendored tau2 banking_knowledge tasks.json")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="output eval_data/ directory (one subdirectory per sample)")
    args = ap.parse_args()

    tasks = json.loads(args.src.read_text())

    # We do not split the cast tasks: all upstream banking tasks are used as held-out TEST cases
    # (the benchmark evaluates artifacts, it does not train on these). Deterministic order: sort by
    # numeric upstream id so re-runs are byte-identical.
    selected = sorted(tasks, key=_sort_key)
    samples = [to_sample(t) for t in selected]
    write_samples(samples, args.out)

    # ---- console summary (should match tasks/banking_support/seed_data/PROVENANCE.md) ----
    from collections import Counter
    expected_list = [json.loads(s["expected_output"]["json"]) for s in samples]
    basis_dist = Counter(tuple(sorted(e["reward_basis"])) for e in expected_list)
    action_dist = Counter(a["name"] for e in expected_list for a in e["actions"])
    user_tool_dist = Counter(u for e in expected_list for u in e["user_tools"])
    n_initial_state = sum(1 for e in expected_list if e["initial_state"] is not None)
    n_comm = sum(1 for e in expected_list if e["communicate_info"])
    n_writeless = sum(
        1 for e in expected_list
        if not any(a["name"] not in _READ_TOOLS for a in e["actions"])
    )

    print(f"wrote {len(samples)} banking eval samples to {args.out}")
    print(f"  reward_basis distribution: {dict(basis_dist)}")
    print(f"  cases with a per-sample initial_state: {n_initial_state}")
    print(f"  cases with communicate_info (expected 0): {n_comm}")
    print(f"  cases whose oracle has no write action: {n_writeless}")
    print("  customer-held user_tools distribution:")
    for name, c in user_tool_dist.most_common():
        print(f"    {name:34s} {c}")
    print("  golden action distribution:")
    for name, c in action_dist.most_common():
        print(f"    {name:34s} {c}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Cast tau2-bench *airline* tasks into appsci-bench eval samples.

WHY
  The airline task (tasks/airline_rebooking/spec.yaml) is an open-source task derived from the
  tau2-bench airline domain (https://github.com/sierra-research/tau2-bench). Each upstream task is a
  customer-service scenario (book / modify / cancel / refund a reservation) with a GOLDEN ground
  truth: the tool calls the agent should end up making (`actions`) + facts it must say to the user
  (`communicate_info`) + NL behavioural assertions (`nl_assertions`). tau2 scores a run as the
  PRODUCT of its `reward_basis` components -- for airline that is DB x COMMUNICATE:
    * DB         -- apply the golden `actions` to a fresh DB; the agent's final DB must hash-match.
    * COMMUNICATE-- every `communicate_info` substring must appear in an agent message to the user.

  This converter is the OFFLINE step the spec's `source` block records. It reads the vendored
  upstream tasks (provenance/airline/tasks.json) and emits eval samples (schema/dataset.schema.json
  $defs/eval_sample), carrying the tau2 ground truth VERBATIM into each sample's `expected_output.json`
  so a future deterministic verifier can replay tau2's exact DB+COMMUNICATE scoring. `eval_rubric`
  carries the NL assertions for the `policy_compliance` LLM judge (tasks/airline_rebooking/eval.py) to
  read; that eval function (not this script) also mirrors the tau2 reward.

FIELD MAPPING (tau2 task -> eval_sample)
  id                                              -> data_point_id (prefixed `tau2_airline_<id>`)
  user_scenario.instructions.known_info           -> multi_turn_input.user_persona (WHO is calling)
  user_scenario.instructions.{reason_for_call,
    unknown_info, task_instructions}              -> multi_turn_input.user_task (WHAT they want,
                                                       composed, customer's voice)
  (upstream sim stop tokens, see USER_STOP_CRITERIA)-> multi_turn_input.user_stop_criteria
  evaluation_criteria.{actions, communicate_info,
    nl_assertions, reward_basis}                  -> expected_output.json (full struct, JSON-encoded)
  evaluation_criteria.{nl_assertions,reward_basis}-> eval_rubric (for policy_compliance's judge)

This is a faithful, deterministic 1:1 cast -- no sampling, no synthesis. Re-running on the same
vendored tasks.json yields byte-identical output (samples sorted by upstream id).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASK_DIR = REPO / "tasks" / "airline_rebooking"
DEFAULT_SRC = REPO / "provenance" / "airline" / "tasks.json"
DEFAULT_OUT = TASK_DIR / "eval_data"

# The airline DB (tau2 FlightDB) materialized as the agent-facing task environment fixture
# (--emit-db). Eval cases point at it by record id; the DB-state oracle is replayed against it.
# The materialized environment/eval/data/db.json IS the canonical, repo-local copy (byte-equal to upstream
# tau2 db.json) -- so re-running --emit-db is idempotent. Point --db-src elsewhere to re-seed it from
# a fresh upstream pull (e.g. /tmp/tau2-bench/data/tau2/domains/airline/db.json).
# The EVAL phase's directory. This is where the full store lives now: the carve gave each phase
# its own data and removed `environment/db/` entirely, so emitting there would recreate a second
# copy that nothing loads and the carve guard does not check.
DEFAULT_DB_DIR = TASK_DIR / "environment" / "eval" / "data"              # materialized fixture
DEFAULT_DB_SRC = DEFAULT_DB_DIR / "db.json"                              # canonical repo-local DB

def _clean(text: str | None) -> str:
    """Collapse upstream newlines/indentation into a single tidy paragraph."""
    if not text:
        return ""
    return " ".join(text.split())


def build_user_persona(task: dict) -> str:
    """WHO is calling -- the upstream `known_info` field verbatim (name/user id/confirmation or
    reservation number, whatever the case includes), cleaned.

    All 50 upstream airline tasks populate `known_info` (verified against the vendored
    provenance/airline/tasks.json), so this is a full mechanical field move, not an NL re-split of
    the composed monologue -- the risk this README/comment used to flag ("only 31/50 samples open
    with a parseable 'You are <Name>...' ") was about regexing the ALREADY-JOINED `user_task`
    string; reading `known_info` directly from tau2's own already-separated field never needed that
    pattern match in the first place. `user_scenario.persona` itself is null for every task (tau2's
    airline domain provides no separate persona field), so there is nothing else to add here.
    """
    instr = task["user_scenario"]["instructions"]
    return _clean(instr.get("known_info"))


def build_user_task(task: dict) -> str:
    """WHAT the caller wants -- why they're calling, what they don't know, and how they'll pursue
    it (negotiating stance, constraints), composed from the upstream instructions. This is what the
    artifact agent user sim would say; it deliberately does NOT leak the golden actions or policy
    outcome. Identity (`known_info`) is NOT included here -- see `build_user_persona`.
    """
    instr = task["user_scenario"]["instructions"]
    parts = []
    if instr.get("reason_for_call"):
        parts.append(f"Reason for the call: {_clean(instr['reason_for_call'])}")
    if instr.get("unknown_info"):
        parts.append(f"What you do not know: {_clean(instr['unknown_info'])}")
    if instr.get("task_instructions"):
        parts.append(f"How you behave: {_clean(instr['task_instructions'])}")
    return " ".join(p for p in parts if p)


def build_eval_rubric(task: dict) -> str:
    """NL rubric for `policy_compliance`'s judge: the tau2 nl_assertions + the reward basis,
    followed by each assertion again as its own bullet (matches the committed sample shape).

    For airline, reward = DB x COMMUNICATE: the final DB must match what the golden actions produce
    AND every required fact must be communicated. The nl_assertions spell out the policy-correct
    behaviour for this case.
    """
    ec = task["evaluation_criteria"]
    asserts = ec.get("nl_assertions") or []
    basis = ec.get("reward_basis") or []
    lines = []
    if asserts:
        lines.append("Agent behaviour: " + " ".join(a.rstrip(".") + "." for a in asserts))
    lines.append(
        f"Scored on tau2 reward_basis {basis} (final reward = product of components): "
        "DB = final database state matches applying the oracle actions to a fresh DB; "
        "COMMUNICATE = every required fact in oracle.data.communicate_info is stated to the user."
    )
    rubric = " ".join(lines)
    if asserts:
        bullets = "\n".join(f"- {a}" for a in asserts)
        rubric += f"\n\nAssertions describing policy-correct behavior for this case:\n{bullets}"
    return rubric


# When the eval-time user sim should end the conversation. Upstream gives its sim three stop tokens
# (data/tau2/user_simulator/simulation_guidelines.md): ###STOP### once the scenario goal is
# satisfied, ###TRANSFER### once the agent has clearly said it is handing the caller to a human, and
# ###OUT-OF-SCOPE### when the scenario provides nothing further to go on. Our sim judges ONE
# free-text criterion instead of emitting tokens, so all three collapse into the clauses below.
#
# Clause (a) is the one place this is an interpretation rather than a transcription: 7 of the 50
# cases are correctly handled by REFUSING, where a bare "the task has been completed" (the framework
# default) never becomes true, so the sim would argue until the ceiling. Treating a settled refusal
# as a resolved outcome is what upstream's sim does in practice when the scenario says the caller
# gives up without a refund; spelling it out here keeps a policy-correct refusal from reading as an
# unfinished conversation now that a truncated trace scores 0.
USER_STOP_CRITERIA = (
    "Any one of the following is true: "
    "(a) what you called about has been resolved one way or the other -- either the agent completed "
    "it, or the agent has clearly refused and you have no further argument to make; "
    "(b) the agent has clearly told you it is transferring you to a human agent (stop once it has "
    "actually said so, not merely offered to); or "
    "(c) you have nothing further to go on, because continuing would need information your scenario "
    "does not give you."
)


def to_sample(task: dict) -> dict:
    """One EvalSample dict (schema/dataset.schema.json $defs/eval_sample) carrying the full tau2
    ground truth. Drives the artifact agent user sim (not built yet) rather than a single query."""
    ec = task["evaluation_criteria"]

    expected = {
        # The authoritative ground truth: applying these write actions to a fresh DB defines the
        # expected final DB state (tau2 'DB' check). Read actions are listed too for traceability.
        "actions": ec.get("actions") or [],
        # Substrings that must appear in an agent message to the user (tau2 'COMMUNICATE' check).
        "communicate_info": ec.get("communicate_info") or [],
        # NL behavioural assertions describing policy-correct handling.
        "nl_assertions": ec.get("nl_assertions") or [],
        # Which components multiply into the tau2 reward (airline = DB x COMMUNICATE).
        "reward_basis": ec.get("reward_basis") or [],
        # Author's note on what the case is probing (purpose), kept for auditability.
        "purpose": _clean((task.get("description") or {}).get("purpose")),
    }

    return {
        "data_point_id": f"tau2_airline_{task['id']}",
        "multi_turn_input": {
            "user_task": build_user_task(task),
            "user_persona": build_user_persona(task),
            "user_stop_criteria": USER_STOP_CRITERIA,
        },
        "expected_output": {"json": json.dumps(expected)},
        "eval_rubric": build_eval_rubric(task),
    }


def materialize_db(db_src: Path, db_dir: Path) -> dict:
    """Write the tau2 airline DB out as the agent-facing task environment fixture.

    Emits `db_dir/db.json` -- the full FlightDB (flights+users+reservations), the fixture the
    environment loads and the DB-state oracle replays against. Returns {collection: count}.
    Deterministic (sorted keys) so re-runs are byte-identical.
    """
    db = json.loads(db_src.read_text())
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "db.json").write_text(json.dumps(db, indent=2, sort_keys=True) + "\n")
    return {coll: len(db.get(coll, {})) for coll in ("flights", "users", "reservations")}


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
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help="vendored tau2 airline tasks.json")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="output eval_data/ directory (one subdirectory per sample)")
    ap.add_argument("--emit-db", action="store_true",
                    help="materialize the airline DB fixture (db.json + per-collection files)")
    ap.add_argument("--db-src", type=Path, default=DEFAULT_DB_SRC, help="vendored tau2 db.json")
    ap.add_argument("--db-dir", type=Path, default=DEFAULT_DB_DIR,
                    help="output dir for the materialized DB fixture")
    args = ap.parse_args()

    tasks = json.loads(args.src.read_text())

    # We do not split the cast tasks: all upstream airline tasks are used as held-out TEST cases
    # (the benchmark evaluates artifacts, it does not train on these). Deterministic order: sort by
    # numeric upstream id so re-runs are byte-identical.
    selected = sorted(tasks, key=lambda t: int(t["id"]) if t["id"].isdigit() else t["id"])
    samples = [to_sample(t) for t in selected]
    write_samples(samples, args.out)

    # ---- optional: materialize the DB fixture ----
    if args.emit_db:
        counts = materialize_db(args.db_src, args.db_dir)
        print(f"materialized DB fixture at {args.db_dir}: "
              + ", ".join(f"{k}={v}" for k, v in counts.items()))

    # ---- console summary ----
    from collections import Counter
    expected_list = [json.loads(s["expected_output"]["json"]) for s in samples]
    action_dist = Counter(a["name"] for e in expected_list for a in e["actions"])
    n_comm = sum(1 for e in expected_list if e["communicate_info"])
    n_writeless = sum(
        1 for e in expected_list
        if not any(a["name"] not in {
            "get_user_details", "get_reservation_details", "search_direct_flight",
            "search_onestop_flight", "list_all_airports", "get_flight_status", "calculate",
        } for a in e["actions"])
    )
    print(f"wrote {len(samples)} airline eval samples to {args.out}")
    print(f"  cases with communicate_info: {n_comm}")
    print(f"  cases whose oracle has no DB-write action (read-only / refusal): {n_writeless}")
    print("  golden action distribution:")
    for name, c in action_dist.most_common():
        print(f"    {name:30s} {c}")


if __name__ == "__main__":
    main()

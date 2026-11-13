#!/usr/bin/env python3
"""Generate 20 self-authored crm_insights eval cases (#4 of the Sep-7 release ruling's five
conditions: "additional eval edge cases", paired with "self-generated eval data").

Unlike `scripts/build_eval_data.py` (which selects CRMArena's own published (query, answer) pairs
verbatim), every question here is originally phrased and every answer/evidence pair is computed by
this script directly against the real fixture data (`seed_data/fixture/*.jsonl` -- CRMArena's real
CRM snapshot, which is fine to use as environment data; see PROVENANCE.md) using the task's own
documented policies (`user_sim_docs/domain_conventions.md`, `case_routing.md`, `handle_time.md`,
`transfer_count.md`). Because those policies are our own document (also used for elicitation
scoring), there is no answer-derivation ambiguity the way there is for the CRMArena-derived 30
(oracle.py's evidence is explicitly best-effort there) -- policy -> answer is exact by construction.

Each `_CASE_ROUTING`/`_HANDLE_TIME`/etc. list below is a fixed, hand-picked set of scenario
parameters (which issue/product/window/agent-filter to use) chosen to (a) resolve cleanly under the
real policy -- no ties left unresolved -- and (b) not duplicate the exact agent/product/issue/state
combination any of the existing 30 CRMArena-derived samples already exercises. The script then
computes the actual answer + evidence from the real data; nothing here is a hardcoded answer.

Usage: python3 tasks/crm_insights/scripts/build_self_generated_eval_cases.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK_DIR = HERE.parent
FIXTURE_DIR = TASK_DIR / "seed_data" / "fixture"
EVAL_DATA_DIR = TASK_DIR / "eval_data"

sys.path.insert(0, str(TASK_DIR / "seed_data"))
import oracle  # noqa: E402 -- reuse its date-window parsing, not reimplement it


def _load_jsonl(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURE_DIR / f"{name}.jsonl").read_text().splitlines() if line.strip()]


def _build_db() -> sqlite3.Connection:
    """In-memory sqlite db with the same table/column names oracle.py's queries already expect
    (i.e. the original crmarena_data.db schema, not the lower_snake_cased Postgres mapping
    environment/db/02_load_data.py uses for the agent's own container)."""
    con = sqlite3.connect(":memory:")
    cur = con.cursor()

    cur.execute('CREATE TABLE "Case" (Id TEXT, Priority TEXT, Subject TEXT, Description TEXT, '
                'Status TEXT, ContactId TEXT, CreatedDate TEXT, ClosedDate TEXT, '
                '"OrderItemId__c" TEXT, "IssueId__c" TEXT, AccountId TEXT, OwnerId TEXT)')
    cur.executemany('INSERT INTO "Case" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', [
        (r.get("Id"), r.get("Priority"), r.get("Subject"), r.get("Description"), r.get("Status"),
         r.get("ContactId"), r.get("CreatedDate"), r.get("ClosedDate"), r.get("OrderItemId__c"),
         r.get("IssueId__c"), r.get("AccountId"), r.get("OwnerId"))
        for r in _load_jsonl("case")
    ])

    cur.execute('CREATE TABLE Account (Id TEXT, ShippingState TEXT)')
    cur.executemany('INSERT INTO Account VALUES (?,?)',
                     [(r.get("Id"), r.get("ShippingState")) for r in _load_jsonl("account")])

    cur.execute('CREATE TABLE "CaseHistory__c" (Id TEXT, "CaseId__c" TEXT, "OldValue__c" TEXT, '
                '"NewValue__c" TEXT, CreatedDate TEXT, "Field__c" TEXT)')
    cur.executemany('INSERT INTO "CaseHistory__c" VALUES (?,?,?,?,?,?)', [
        (r.get("Id"), r.get("CaseId__c"), r.get("OldValue__c"), r.get("NewValue__c"),
         r.get("CreatedDate"), r.get("Field__c"))
        for r in _load_jsonl("casehistory")
    ])

    cur.execute('CREATE TABLE OrderItem (Id TEXT, Product2Id TEXT)')
    cur.executemany('INSERT INTO OrderItem VALUES (?,?)',
                     [(r.get("Id"), r.get("Product2Id")) for r in _load_jsonl("orderitem")])

    con.commit()
    return con


def _parse_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")


def _never_transferred_ids(con: sqlite3.Connection) -> set[str]:
    """Cases with exactly one 'Owner Assignment' history row -- handle_time.md / transfer_count.md's
    filter for which cases count toward a "managed X cases" claim and toward handle-time averages."""
    rows = con.execute(
        'SELECT "CaseId__c", COUNT(*) FROM "CaseHistory__c" WHERE "Field__c" = \'Owner Assignment\' '
        'GROUP BY "CaseId__c"'
    ).fetchall()
    return {cid for cid, n in rows if n == 1}


# --- case_routing -----------------------------------------------------------------------------

# (issue name for phrasing, issue id, product name, product id, fresh case description)
_CASE_ROUTING = [
    ("Product Defect", "a1MWs0000012tz7MAA", "Hiking Waterproof Boots", "01tWs000002wRhpIAE",
     "Unusable Hiking Boots After One Trip",
     "The pair of Hiking Waterproof Boots I bought started falling apart after a single day hike -- "
     "the sole is already separating from the upper on both boots. This looks like a manufacturing "
     "defect, not normal wear. Can someone look into a replacement or refund?"),
    ("Shipping Delay", "a1MWs0000012u2LMAQ", "Trailblazer Jacket", "01tWs000002wQp0IAE",
     "Trailblazer Jacket Order Stuck in Transit",
     "I ordered the Trailblazer Jacket over two weeks ago and the tracking page hasn't updated in "
     "10 days -- it still says 'label created'. I need this jacket for a trip this weekend. Can you "
     "check what's going on with the shipment?"),
    ("Warranty Issue", "a1MWs0000012u5ZMAQ", "Velocity Basketball Shoes", "01tWs000002wRZlIAM",
     "Sole Separated on Velocity Basketball Shoes Within Warranty Period",
     "The sole on my Velocity Basketball Shoes came completely apart from the upper after about two "
     "months of normal use. I believe this is covered under the product warranty -- how do I file a "
     "claim for a replacement pair?"),
]


def case_routing_cases(con: sqlite3.Connection) -> list[dict]:
    out = []
    for idx, (issue_name, issue_id, product_name, product_id, subject, description) in enumerate(_CASE_ROUTING, start=1):
        # Tier 1: issue expertise -- most closed cases for this issue.
        tier1 = con.execute(
            'SELECT OwnerId, COUNT(*) c FROM "Case" WHERE Status = \'Closed\' AND "IssueId__c" = ? '
            'GROUP BY OwnerId ORDER BY c DESC', (issue_id,)
        ).fetchall()
        top_n = tier1[0][1]
        tied = [a for a, c in tier1 if c == top_n]

        winner = tied[0]
        if len(tied) > 1:
            # Tier 2: product expertise tie-break -- most closed cases for the given product.
            q = ('SELECT c.OwnerId, COUNT(*) FROM "Case" c JOIN OrderItem oi ON c."OrderItemId__c" = oi.Id '
                 'WHERE oi.Product2Id = ? AND c.Status = \'Closed\' AND c.OwnerId IN ({}) '
                 'GROUP BY c.OwnerId').format(",".join("?" * len(tied)))
            pe = dict(con.execute(q, [product_id] + tied).fetchall())
            pe = {a: pe.get(a, 0) for a in tied}
            max_pe = max(pe.values())
            tied = [a for a, v in pe.items() if v == max_pe]
            winner = tied[0]
            if len(tied) > 1:
                # Tier 3: workload tie-break -- fewest non-Closed cases.
                q = ('SELECT OwnerId, COUNT(*) FROM "Case" WHERE Status != \'Closed\' AND OwnerId IN ({}) '
                     'GROUP BY OwnerId').format(",".join("?" * len(tied)))
                wl = dict(con.execute(q, tied).fetchall())
                wl = {a: wl.get(a, 0) for a in tied}
                winner = min(wl, key=wl.get)

        evidence = [r[0] for r in con.execute(
            'SELECT Id FROM "Case" WHERE Status = \'Closed\' AND "IssueId__c" = ? AND OwnerId = ? ORDER BY Id',
            (issue_id, winner)
        ).fetchall()]

        out.append({
            "data_point_id": f"crm_insights__case_routing__self{idx:02d}",
            "single_turn_input": {"text": (
                "Use the case routing policy to determine the most suitable agent for the given case. "
                "Return only the Id of the agent.\n## Given Case\n"
                f"- Case Subject: {subject}\n- Case Description: {description}"
            )},
            "expected_output": {"json": json.dumps({
                "answer": winner, "evidence_case_ids": evidence,
                "task_category": "case_routing", "reward_metric": "exact_match",
            })},
        })
    return out


# --- handle_time -------------------------------------------------------------------------------

# (window start, window end, min_case_count [strictly more than], direction, phrasing, today)
_HANDLE_TIME = [
    (date(2020, 1, 1), date(2024, 6, 15), 1, "shortest",
     "Find the agent with the shortest handle time who managed more than one case.", "2024-06-15"),
    (date(2020, 1, 1), date(2024, 6, 15), 5, "shortest",
     "Find the agent with the shortest handle time who managed more than five cases.", "2024-06-15"),
    (date(2022, 1, 1), date(2022, 12, 31), 1, "longest",
     "Find the agent with the longest handle time who managed more than one case in 2022.", "2023-01-15"),
    (date(2021, 9, 1), date(2021, 11, 30), 1, "longest",
     "Find the agent with the longest handle time who managed more than one case in Fall 2021.", "2022-01-10"),
]


def handle_time_cases(con: sqlite3.Connection) -> list[dict]:
    never_transferred = _never_transferred_ids(con)
    rows = con.execute('SELECT Id, OwnerId, Status, CreatedDate, ClosedDate FROM "Case"').fetchall()

    out = []
    for idx, (start, end, min_n, direction, text, today) in enumerate(_HANDLE_TIME, start=1):
        per_agent: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for cid, owner, status, created, closed in rows:
            if cid not in never_transferred or status != "Closed" or not closed:
                continue
            if not (start <= date.fromisoformat(closed[:10]) <= end):
                continue
            dur_hours = (_parse_dt(closed) - _parse_dt(created)).total_seconds() / 3600
            per_agent[owner].append((cid, dur_hours))

        candidates = [(sum(d for _, d in lst) / len(lst), agent, [c for c, _ in lst])
                      for agent, lst in per_agent.items() if len(lst) > min_n]
        assert candidates, f"handle_time self{idx:02d}: no qualifying agent"
        candidates.sort(key=lambda x: x[0])
        winner_avg, winner, evidence = candidates[0] if direction == "shortest" else candidates[-1]
        runner_up_avg = candidates[1][0] if direction == "shortest" else candidates[-2][0]
        assert winner_avg != runner_up_avg, f"handle_time self{idx:02d}: unresolved tie"

        out.append({
            "data_point_id": f"crm_insights__handle_time__self{idx:02d}",
            "single_turn_input": {"text": (
                f"{text} Return only the Id of the agent.\n\n(Today's date: {today})"
            )},
            "expected_output": {"json": json.dumps({
                "answer": winner, "evidence_case_ids": sorted(evidence),
                "task_category": "handle_time", "reward_metric": "exact_match",
            })},
        })
    return out


# --- transfer_count ----------------------------------------------------------------------------

# (n, unit, today) -- window is derived via oracle.parse_window, exactly like the real pipeline
_TRANSFER_COUNT = [
    (9, "month", "2021-04-01"),
    (6, "quarter", "2023-09-01"),
    (11, "quarter", "2023-03-01"),
]


def transfer_count_cases(con: sqlite3.Connection) -> list[dict]:
    hist = con.execute(
        'SELECT "CaseId__c", "OldValue__c", CreatedDate FROM "CaseHistory__c" '
        'WHERE "Field__c" = \'Owner Assignment\' AND "OldValue__c" IS NOT NULL'
    ).fetchall()

    out = []
    for idx, (n, unit, today) in enumerate(_TRANSFER_COUNT, start=1):
        query_text = f"Identify the agent with the highest number of transfers over the past {n} {unit}s among those who managed cases."
        window = oracle.parse_window(query_text, {"required": f"Today's date: {today}"})
        assert window, f"transfer_count self{idx:02d}: window failed to parse"
        start, end = window

        per_agent: dict[str, list[str]] = defaultdict(list)
        for cid, old, created in hist:
            if start <= date.fromisoformat(created[:10]) <= end:
                per_agent[old].append(cid)

        candidates = sorted(((len(v), a, v) for a, v in per_agent.items()), reverse=True)
        assert len(candidates) >= 2 and candidates[0][0] != candidates[1][0], \
            f"transfer_count self{idx:02d}: unresolved tie"
        _, winner, evidence = candidates[0]

        out.append({
            "data_point_id": f"crm_insights__transfer_count__self{idx:02d}",
            "single_turn_input": {"text": (
                f"{query_text} Return only the Id of the agent.\n\n(Today's date: {today})"
            )},
            "expected_output": {"json": json.dumps({
                "answer": winner, "evidence_case_ids": sorted(evidence),
                "task_category": "transfer_count", "reward_metric": "exact_match",
            })},
        })
    return out


# --- top_issue_identification / monthly_trend_analysis (shared window) -------------------------

_SHARED_WINDOW_N, _SHARED_WINDOW_UNIT, _SHARED_TODAY = 53, "month", "2024-06-15"

_TOP_ISSUE = [
    ("Elemental Soccer Shin Guards", "01tWs000002wRmgIAE"),
    ("Endurance Road Runner", "01tWs000002wS2nIAE"),
    ("Performance Basketball Hoodie", "01tWs000002wSyrIAE"),
]

_MONTHLY_TREND = [
    ("Cyclone Bike Jersey", "01tWs000002wR8MIAU"),
    ("Classic Court Sneakers", "01tWs000002wRQ5IAM"),
    ("Pro Speed Rope", "01tWs000002wROTIA2"),
]


def _shared_window() -> tuple[date, date]:
    win = oracle.parse_window(
        f"past {_SHARED_WINDOW_N} {_SHARED_WINDOW_UNIT}s",
        {"required": f"Today's date: {_SHARED_TODAY}"},
    )
    assert win
    return win


def top_issue_cases(con: sqlite3.Connection) -> list[dict]:
    start, end = _shared_window()
    out = []
    for idx, (product_name, product_id) in enumerate(_TOP_ISSUE, start=1):
        rows = con.execute(
            'SELECT c.Id, c.CreatedDate, c."IssueId__c" FROM "Case" c '
            'JOIN OrderItem oi ON c."OrderItemId__c" = oi.Id WHERE oi.Product2Id = ?', (product_id,)
        ).fetchall()
        by_issue: dict[str, list[str]] = defaultdict(list)
        for cid, created, issue_id in rows:
            if start <= date.fromisoformat(created[:10]) <= end:
                by_issue[issue_id].append(cid)
        candidates = sorted(((len(v), i, v) for i, v in by_issue.items()), reverse=True)
        assert len(candidates) >= 2 and candidates[0][0] != candidates[1][0], \
            f"top_issue self{idx:02d}: unresolved tie"
        _, winner, evidence = candidates[0]

        out.append({
            "data_point_id": f"crm_insights__top_issue_identification__self{idx:02d}",
            "single_turn_input": {"text": (
                f"Over the past {_SHARED_WINDOW_N} months, what is the most frequently reported issue "
                f"for the {product_name}? The associated product Id is {product_id}. Return only the "
                "issue Id of the most reported issue for this product.\n\n"
                f"(Today's date: {_SHARED_TODAY})"
            )},
            "expected_output": {"json": json.dumps({
                "answer": winner, "evidence_case_ids": sorted(evidence),
                "task_category": "top_issue_identification", "reward_metric": "exact_match",
            })},
        })
    return out


def monthly_trend_cases(con: sqlite3.Connection) -> list[dict]:
    start, end = _shared_window()
    out = []
    for idx, (product_name, product_id) in enumerate(_MONTHLY_TREND, start=1):
        rows = con.execute(
            'SELECT c.Id, c.CreatedDate FROM "Case" c '
            'JOIN OrderItem oi ON c."OrderItemId__c" = oi.Id WHERE oi.Product2Id = ?', (product_id,)
        ).fetchall()
        by_month: dict[str, list[str]] = defaultdict(list)
        for cid, created in rows:
            d = date.fromisoformat(created[:10])
            if start <= d <= end:
                by_month[d.strftime("%B")].append(cid)
        candidates = sorted(((len(v), m, v) for m, v in by_month.items()), reverse=True)
        assert len(candidates) >= 2 and candidates[0][0] != candidates[1][0], \
            f"monthly_trend self{idx:02d}: unresolved tie"
        _, winner_month, evidence = candidates[0]

        out.append({
            "data_point_id": f"crm_insights__monthly_trend_analysis__self{idx:02d}",
            "single_turn_input": {"text": (
                f"Over the past {_SHARED_WINDOW_N} months, is there a particular month where we "
                f"received significantly more cases for the {product_name} compared to other months? "
                f"The associated product Id is {product_id}. Return only the month name.\n\n"
                f"(Today's date: {_SHARED_TODAY})"
            )},
            "expected_output": {"json": json.dumps({
                "answer": winner_month, "evidence_case_ids": sorted(evidence),
                "task_category": "monthly_trend_analysis", "reward_metric": "exact_match",
            })},
        })
    return out


# --- best_region_identification ------------------------------------------------------------------

# (n, unit, today)
_BEST_REGION = [
    (5, "month", "2024-03-01"),
    (4, "month", "2020-12-01"),
    (4, "quarter", "2022-09-01"),
    (8, "month", "2022-01-01"),
]


def best_region_cases(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        'SELECT c.Id, c.CreatedDate, c.ClosedDate, a.ShippingState FROM "Case" c '
        'JOIN Account a ON c.AccountId = a.Id WHERE c.Status = \'Closed\''
    ).fetchall()

    out = []
    for idx, (n, unit, today) in enumerate(_BEST_REGION, start=1):
        window = oracle.parse_window(f"past {n} {unit}s", {"required": f"Today's date: {today}"})
        assert window
        start, end = window

        per_state: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for cid, created, closed, state in rows:
            if not (closed and state):
                continue
            if start <= date.fromisoformat(closed[:10]) <= end:
                dur_hours = (_parse_dt(closed) - _parse_dt(created)).total_seconds() / 3600
                per_state[state].append((cid, dur_hours))

        candidates = [(sum(d for _, d in lst) / len(lst), state, [c for c, _ in lst])
                      for state, lst in per_state.items() if len(lst) >= 3]
        assert len(candidates) >= 2, f"best_region self{idx:02d}: too few qualifying states"
        candidates.sort(key=lambda x: x[0])
        winner_avg, winner_state, evidence = candidates[0]
        assert winner_avg != candidates[1][0], f"best_region self{idx:02d}: unresolved tie"

        out.append({
            "data_point_id": f"crm_insights__best_region_identification__self{idx:02d}",
            "single_turn_input": {"text": (
                f"Which states had the quickest case closures over the past {n} {unit}s? Return only "
                "the two-letter abbreviation of the most matching state (eg. CA).\n\n"
                f"(Today's date: {today})"
            )},
            "expected_output": {"json": json.dumps({
                "answer": winner_state, "evidence_case_ids": sorted(evidence),
                "task_category": "best_region_identification", "reward_metric": "exact_match",
            })},
        })
    return out


def main() -> None:
    con = _build_db()
    all_cases = (
        case_routing_cases(con) + handle_time_cases(con) + transfer_count_cases(con)
        + top_issue_cases(con) + monthly_trend_cases(con) + best_region_cases(con)
    )
    assert len(all_cases) == 20, f"expected 20 cases, got {len(all_cases)}"

    for sample in all_cases:
        d = EVAL_DATA_DIR / sample["data_point_id"]
        d.mkdir(exist_ok=True)
        (d / "sample.json").write_text(json.dumps(sample, indent=2) + "\n")

    by_cat: dict[str, int] = defaultdict(int)
    for s in all_cases:
        by_cat[json.loads(s["expected_output"]["json"])["task_category"]] += 1
    print(f"wrote {len(all_cases)} self-generated eval samples -> {EVAL_DATA_DIR}")
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat}: {n}")


if __name__ == "__main__":
    main()

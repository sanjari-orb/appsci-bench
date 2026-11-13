"""crm_insights evidence derivation.

CRMArena's published (query, answer) pairs (Salesforce/CRMArena, filtered to the 6 analyst/manager
categories -- see PROVENANCE.md) are used as-is for the `answer_correctness` objective; the
`answer` field is real ground truth, not something we recompute. What CRMArena does NOT publish is
which records justify a given answer, and `grounding` (spec.yaml) needs that.

This module derives a best-effort supporting-record set (Case IDs, mostly) per category, by
parsing the query's stated time window and re-deriving the aggregate over the snapshot
(`crmarena_data.db`, a byte-for-byte copy of CRMArena's own checked-in local data mirror -- see
PROVENANCE.md). It is deliberately best-effort, not a guaranteed exact reproduction of CRMArena's
own (undocumented) window/tie-break conventions -- see the parsing-coverage note in
PROVENANCE.md. `grounding` is scored as a tolerant overlap (eval.py), not exact-set-equality, so an
approximate-but-reasonable evidence set is the right level of precision here.
"""
from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date, datetime
from difflib import SequenceMatcher

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
SEASON_MONTHS = {  # (start_month, end_month) inclusive; winter spans the year boundary
    "winter": (12, 2), "spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11),
}


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()


def _today(metadata: dict) -> date | None:
    m = re.search(r"Today's date:\s*(\d{4}-\d{2}-\d{2})", metadata.get("required", "") or "")
    return date.fromisoformat(m.group(1)) if m else None


def parse_window(query: str, metadata: dict) -> tuple[date, date] | None:
    """Best-effort (start, end_inclusive) window from the query text. Returns None if unparseable
    with the patterns below (caller should skip / fall back for that sample)."""
    q = query.lower()

    m = re.search(r"(january|february|march|april|may|june|july|august|september|october|"
                  r"november|december)\s+(\d{4})", q)
    if m:
        month, year = MONTHS[m.group(1)], int(m.group(2))
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        return start, end

    m = re.search(r"(winter|spring|summer|fall|autumn)\s+(?:of\s+)?(\d{4})", q)
    if m:
        season, year = m.group(1), int(m.group(2))
        start_m, end_m = SEASON_MONTHS[season]
        if season == "winter":  # Dec of the PRIOR year through Feb of the named year.
            start = date(year - 1, 12, 1)
            end = date(year, 2, calendar.monthrange(year, 2)[1])
        else:
            start = date(year, start_m, 1)
            end = date(year, end_m, calendar.monthrange(year, end_m)[1])
        return start, end

    m = re.search(r"(?:past|last|over the last)\s+(\d+)\s+(month|quarter)s?", q)
    if m:
        today = _today(metadata)
        if not today:
            return None
        n, unit = int(m.group(1)), m.group(2)
        n_months = n * (3 if unit == "quarter" else 1)
        end = today
        y, mo = end.year, end.month - n_months
        while mo <= 0:
            mo += 12
            y -= 1
        start = date(y, mo, 1)
        return start, end

    return None


def _in_window(d: date | None, window: tuple[date, date]) -> bool:
    return d is not None and window[0] <= d <= window[1]


def _cases(con: sqlite3.Connection, where: str = "1=1", params: tuple = ()) -> list[dict]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(f'SELECT * FROM "Case" WHERE {where}', params)]


def handle_time_evidence(row: dict, con: sqlite3.Connection) -> list[str]:
    window = parse_window(row["query"], row["metadata"])
    if not window:
        return []
    agent = row["answer"]
    return [c["Id"] for c in _cases(con, "OwnerId = ? AND Status = 'Closed'", (agent,))
            if _in_window(_parse_date(c["ClosedDate"]), window)]


def transfer_count_evidence(row: dict, con: sqlite3.Connection) -> list[str]:
    window = parse_window(row["query"], row["metadata"])
    if not window:
        return []
    agent = row["answer"]
    con.row_factory = sqlite3.Row
    hist = con.execute(
        'SELECT * FROM CaseHistory__c WHERE Field__c = \'Owner Assignment\' AND OldValue__c = ?',
        (agent,),
    ).fetchall()
    return [dict(h)["CaseId__c"] for h in hist if _in_window(_parse_date(dict(h)["CreatedDate"]), window)]


def top_issue_evidence(row: dict, con: sqlite3.Connection) -> list[str]:
    pm = re.search(r"[Pp]roduct\s*Id\s*is\s*(\w+)", row["query"])
    window = parse_window(row["query"], row["metadata"])
    if not pm or not window:
        return []
    product_id, issue_id = pm.group(1), row["answer"]
    con.row_factory = sqlite3.Row
    rows = con.execute(
        'SELECT c.Id as Id, c.CreatedDate as CreatedDate FROM "Case" c '
        'JOIN OrderItem oi ON c.OrderItemId__c = oi.Id '
        'WHERE oi.Product2Id = ? AND c.IssueId__c = ?', (product_id, issue_id),
    ).fetchall()
    return [dict(r)["Id"] for r in rows if _in_window(_parse_date(dict(r)["CreatedDate"]), window)]


def best_region_evidence(row: dict, con: sqlite3.Connection) -> list[str]:
    window = parse_window(row["query"], row["metadata"])
    if not window:
        return []
    state = row["answer"]
    con.row_factory = sqlite3.Row
    rows = con.execute(
        'SELECT c.Id as Id, c.ClosedDate as ClosedDate FROM "Case" c '
        'JOIN Account a ON c.AccountId = a.Id '
        "WHERE a.ShippingState = ? AND c.Status = 'Closed'", (state,),
    ).fetchall()
    return [dict(r)["Id"] for r in rows if _in_window(_parse_date(dict(r)["ClosedDate"]), window)]


def monthly_trend_evidence(row: dict, con: sqlite3.Connection) -> list[str]:
    pm = re.search(r"[Pp]roduct\s*Id\s*is\s*(\w+)", row["query"])
    window = parse_window(row["query"], row["metadata"])
    month_name = row["answer"]
    if not pm or not window or month_name.lower() not in MONTHS:
        return []
    product_id, month = pm.group(1), MONTHS[month_name.lower()]
    con.row_factory = sqlite3.Row
    rows = con.execute(
        'SELECT c.Id as Id, c.CreatedDate as CreatedDate FROM "Case" c '
        'JOIN OrderItem oi ON c.OrderItemId__c = oi.Id '
        'WHERE oi.Product2Id = ?', (product_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = _parse_date(dict(r)["CreatedDate"])
        if d and _in_window(d, window) and d.month == month:
            out.append(dict(r)["Id"])
    return out


def _issue_similarity_evidence(row: dict, con: sqlite3.Connection) -> list[str]:
    """case_routing: no clean lookup exists (the query poses a NEW hypothetical case, and 'most
    similar issue' is CRMArena's own semantic judgment, not a DB fact -- see PROVENANCE.md). Falls
    back to the winning agent's full closed-case load as a defensible-but-approximate evidence set
    (\"why this agent\" = their case history), rather than trying to fabricate exact issue-matching.
    """
    agent = row["answer"]
    return [c["Id"] for c in _cases(con, "OwnerId = ? AND Status = 'Closed'", (agent,))]


EVIDENCE_FNS = {
    "handle_time": handle_time_evidence,
    "transfer_count": transfer_count_evidence,
    "top_issue_identification": top_issue_evidence,
    "monthly_trend_analysis": monthly_trend_evidence,
    "best_region_identification": best_region_evidence,
    "case_routing": _issue_similarity_evidence,
}


def evidence_for(row: dict, con: sqlite3.Connection) -> list[str]:
    fn = EVIDENCE_FNS.get(row["task"])
    return fn(row, con) if fn else []

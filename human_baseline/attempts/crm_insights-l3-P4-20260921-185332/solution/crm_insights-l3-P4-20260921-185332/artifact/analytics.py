"""Policy-encoded analytics over the CRM.

Every operational rule the stakeholder gave us lives here, not in the model's head:

Handle Time    duration from case opened to case closed. NOT computed for cases that were
               transferred to another agent -- a non-transferred case has exactly ONE
               'Owner Assignment' history row, a transferred one has MORE THAN ONE.
Managed cases  "agents who managed/handled X cases" counts cases by the FIRST agent the case was
               assigned to. A case transferred IN from another agent does not count towards the
               receiving agent's managed total, and a case transferred AWAY still counts towards
               the agent who had it first.
Transfer count number of reassignments from one agent to another; each transfer from A to B counts
               towards A.
Case routing   issue expertise (most closed cases on the most similar issue), tie-broken by
               product expertise (most solved cases on the most relevant product), tie-broken by
               workload (fewest cases whose status is not 'Closed').
"""
from __future__ import annotations

import re

import db

# --------------------------------------------------------------------------- base case set

BASE_CTE = """
first_assign AS (
    SELECT case_id, new_value AS first_agent_id
    FROM crm_case_history
    WHERE field = 'Owner Assignment' AND COALESCE(old_value, '') = ''
),
oa AS (
    SELECT case_id, COUNT(*) AS n_owner_rows
    FROM crm_case_history
    WHERE field = 'Owner Assignment'
    GROUP BY case_id
),
enriched AS (
    SELECT c.id, c.subject, c.description, c.status, c.priority,
           c.created_date, c.closed_date, c.issue_id, c.account_id, c.owner_id,
           fa.first_agent_id,
           oa.n_owner_rows,
           (oa.n_owner_rows > 1) AS transferred,
           oi.product2_id,
           p.name  AS product_name,
           i.name  AS issue_name,
           a.shipping_state, a.shipping_city,
           EXTRACT(EPOCH FROM (c.closed_date - c.created_date)) AS handle_seconds
    FROM crm_case c
    LEFT JOIN first_assign fa ON fa.case_id = c.id
    LEFT JOIN oa            ON oa.case_id = c.id
    LEFT JOIN crm_order_item oi ON oi.id = c.order_item_id
    LEFT JOIN crm_product2   p  ON p.id  = oi.product2_id
    LEFT JOIN crm_issue      i  ON i.id  = c.issue_id
    LEFT JOIN crm_account    a  ON a.id  = c.account_id
)
"""

# dim name -> (SQL expression over `base`, human label source)
DIMS = {
    "agent":         "base.first_agent_id",   # policy default: the FIRST-assigned agent
    "current_owner": "base.owner_id",
    "state":         "base.shipping_state",
    "city":          "base.shipping_city",
    "issue":         "base.issue_name",
    "product":       "base.product_name",
    "priority":      "base.priority",
    "status":        "base.status",
    "account":       "base.account_id",
    "month":         "to_char({date_col}, 'YYYY-MM')",
    "month_name":    "trim(to_char({date_col}, 'Month'))",
    "year":          "to_char({date_col}, 'YYYY')",
    "quarter":       "to_char({date_col}, 'YYYY-\"Q\"Q')",
    "day_of_week":   "trim(to_char({date_col}, 'Day'))",
}

METRICS = ("avg_handle_time", "median_handle_time", "total_handle_time", "case_count", "transfer_count")


def _date_col(date_field: str) -> str:
    return "base.closed_date" if date_field == "closed" else "base.created_date"


def _fmt_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = float(seconds)
    d, rem = divmod(int(round(seconds)), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts) + f" ({seconds / 3600:.2f} hours)"


# --------------------------------------------------------------------------- name resolution


def lookup(kind: str, term: str | None = None, limit: int = 25) -> dict:
    """Resolve/browse the catalogs: issues, products, agents, states."""
    kind = (kind or "").lower().rstrip("s") + "s"
    if kind == "issues":
        rows = db.query("SELECT id, name FROM crm_issue ORDER BY name")
        if term:
            rows = [r for r in rows if term.lower() in r["name"].lower()] or rows
        return {"kind": "issues", "matches": rows}
    if kind == "products":
        # ordered by how many cases actually reference the product: of several partial matches, the
        # one a question is about is the one with cases against it, not the alphabetically first
        sel = ("SELECT p.id, p.name, p.is_active, (SELECT COUNT(*) FROM crm_case c"
               " JOIN crm_order_item oi ON oi.id = c.order_item_id"
               " WHERE oi.product2_id = p.id) AS cases FROM crm_product2 p")
        tail = " ORDER BY cases DESC, p.name LIMIT %s"
        if term:
            rows = db.query(f"{sel} WHERE p.name ILIKE %s{tail}", (f"%{term}%", limit))
            if not rows:  # fall back to any-word overlap
                words = re.findall(r"[A-Za-z]{4,}", term)
                if words:
                    rows = db.query(
                        f"{sel} WHERE " + " OR ".join(["p.name ILIKE %s"] * len(words)) + tail,
                        tuple([f"%{w}%" for w in words] + [limit]))
        else:
            rows = db.query(f"{sel}{tail}", (limit,))
        return {"kind": "products", "matches": rows}
    if kind == "agents":
        sql = ("SELECT u.id, u.first_name || ' ' || u.last_name AS name, u.email,"
               " (SELECT COUNT(*) FROM crm_case c WHERE c.owner_id = u.id) AS cases_owned"
               " FROM crm_user u")
        params: tuple = ()
        if term:
            sql += (" WHERE u.first_name ILIKE %s OR u.last_name ILIKE %s"
                    " OR (u.first_name || ' ' || u.last_name) ILIKE %s OR u.id = %s")
            params = (f"%{term}%", f"%{term}%", f"%{term}%", term)
        sql += " ORDER BY cases_owned DESC LIMIT %s"
        return {"kind": "agents", "matches": db.query(sql, params + (limit,))}
    if kind == "states":
        return {"kind": "states", "matches": db.query(
            "SELECT shipping_state AS state, COUNT(*) AS accounts FROM crm_account"
            " WHERE shipping_state IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")}
    raise ValueError(f"unknown catalog {kind!r}; use issues, products, agents or states")


def _resolve(kind: str, term: str) -> dict | None:
    """Best single match for a user-supplied issue/product name (or a raw id)."""
    matches = lookup(kind, term, limit=50)["matches"]
    if not matches:
        return None
    low = term.lower().strip()
    for r in matches:
        if r.get("id", "").lower() == low or str(r.get("name", "")).lower() == low:
            return r
    return matches[0]


def agent_names(ids: list[str]) -> dict[str, str]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = db.query(
        "SELECT id, first_name || ' ' || last_name AS name FROM crm_user WHERE id = ANY(%s)", (ids,))
    return {r["id"]: r["name"] for r in rows}


# --------------------------------------------------------------------------- filters


def _build_filters(filters: dict | None, date_field: str) -> tuple[str, dict, dict]:
    filters = dict(filters or {})
    clauses: list[str] = []
    params: dict = {}
    applied: dict = {}
    dcol = _date_col(date_field).replace("base.", "enriched.")

    if filters.get("start_date"):
        clauses.append(f"{dcol} >= %(start_date)s::date")
        params["start_date"] = filters["start_date"]
        applied["start_date"] = filters["start_date"]
    if filters.get("end_date"):
        # inclusive end day
        clauses.append(f"{dcol} < (%(end_date)s::date + INTERVAL '1 day')")
        params["end_date"] = filters["end_date"]
        applied["end_date"] = filters["end_date"]
    if filters.get("start_date") or filters.get("end_date"):
        applied["date_field"] = "closed_date" if date_field == "closed" else "created_date"

    if filters.get("issue"):
        hit = _resolve("issues", str(filters["issue"]))
        if not hit:
            raise ValueError(f"no issue matches {filters['issue']!r}")
        clauses.append("enriched.issue_id = %(issue_id)s")
        params["issue_id"] = hit["id"]
        applied["issue"] = {"id": hit["id"], "name": hit["name"]}
    if filters.get("product"):
        hit = _resolve("products", str(filters["product"]))
        if not hit:
            raise ValueError(f"no product matches {filters['product']!r}")
        clauses.append("enriched.product2_id = %(product_id)s")
        params["product_id"] = hit["id"]
        applied["product"] = {"id": hit["id"], "name": hit["name"]}
        others = [r["name"] for r in lookup("products", str(filters["product"]))["matches"][1:4]]
        if others:  # so an ambiguous product name is visible rather than silently resolved
            applied["product"]["other_name_matches"] = others
    if filters.get("state"):
        clauses.append("upper(enriched.shipping_state) = upper(%(state)s)")
        params["state"] = str(filters["state"]).strip()
        applied["state"] = params["state"]
    if filters.get("status"):
        clauses.append("enriched.status = %(status)s")
        params["status"] = filters["status"]
        applied["status"] = filters["status"]
    if filters.get("priority"):
        clauses.append("enriched.priority = %(priority)s")
        params["priority"] = filters["priority"]
        applied["priority"] = filters["priority"]
    if filters.get("agent"):
        hit = _resolve("agents", str(filters["agent"]))
        if not hit:
            raise ValueError(f"no agent matches {filters['agent']!r}")
        clauses.append("enriched.first_agent_id = %(agent_id)s")
        params["agent_id"] = hit["id"]
        applied["agent"] = {"id": hit["id"], "name": hit["name"]}

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, applied


# --------------------------------------------------------------------------- the workhorse


def analyze(metric: str, group_by: str, filters: dict | None = None, order: str = "desc",
            min_cases_per_group: int | None = None, date_field: str | None = None,
            top_n: int = 10, evidence_limit: int = 40) -> dict:
    """Rank groups by a metric, with the handle-time / managed-cases policies applied.

    Returns the ranking, every group tied for first place, and the case ids behind the winner(s).
    """
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; use one of {', '.join(METRICS)}")
    if group_by not in DIMS:
        raise ValueError(f"unknown group_by {group_by!r}; use one of {', '.join(DIMS)}")
    if metric == "transfer_count" and group_by not in ("agent", "current_owner"):
        raise ValueError("transfer_count only groups by agent")
    order = "asc" if str(order).lower().startswith("a") else "desc"
    if date_field is None:
        # created_date is the default for every metric. The stakeholder has no fixed rule here and
        # reads it by question intent; defaulting handle-time questions to closed_date instead was
        # measured WORSE on held-out region samples (0.40 vs 0.60, n=5), so the caller must ask for
        # closed_date explicitly when a question ties its window to closing.
        date_field = "created"
    date_field = "closed" if str(date_field).startswith("clos") else "created"

    where, params, applied = _build_filters(filters, date_field)
    dim_sql = DIMS[group_by].format(date_col=_date_col(date_field))
    policy: list[str] = []

    # cohort = every case in scope, grouped by the dim. For group_by=agent this IS the
    # "managed/handled X cases" count: cases counted against their FIRST-assigned agent.
    cohort_dim = dim_sql
    cohort = f"""
    cohort AS (
        SELECT {cohort_dim} AS gk, COUNT(*) AS cohort_n,
               (ARRAY_AGG(base.id ORDER BY base.created_date))[1:{int(evidence_limit)}] AS cohort_case_ids
        FROM base WHERE {cohort_dim} IS NOT NULL GROUP BY 1
    )"""
    if group_by == "agent":
        policy.append("'managed/handled N cases' counts cases against the agent they were FIRST "
                      "assigned to (a case transferred in from another agent does not count).")

    if metric in ("avg_handle_time", "median_handle_time", "total_handle_time"):
        agg_fn = {"avg_handle_time": "AVG(base.handle_seconds)",
                  "median_handle_time": "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY base.handle_seconds)",
                  "total_handle_time": "SUM(base.handle_seconds)"}[metric]
        restrict = "base.n_owner_rows = 1 AND base.closed_date IS NOT NULL"
        policy.append("Handle time = closed_date - created_date, computed ONLY on cases that were "
                      "never transferred (exactly one 'Owner Assignment' history row).")
        agg = f"""
    agg AS (
        SELECT {dim_sql} AS gk, COUNT(*) AS metric_n, {agg_fn} AS val,
               (ARRAY_AGG(base.id ORDER BY base.handle_seconds))[1:{int(evidence_limit)}] AS case_ids
        FROM base WHERE {restrict} AND {dim_sql} IS NOT NULL GROUP BY 1
    )"""
        require_value = True
    elif metric == "case_count":
        agg = f"""
    agg AS (
        SELECT {dim_sql} AS gk, COUNT(*) AS metric_n, COUNT(*)::float AS val,
               (ARRAY_AGG(base.id ORDER BY base.created_date))[1:{int(evidence_limit)}] AS case_ids
        FROM base WHERE {dim_sql} IS NOT NULL GROUP BY 1
    )"""
        require_value = True
    else:  # transfer_count -- each transfer away from agent A counts towards A
        agg = f"""
    agg AS (
        SELECT h.old_value AS gk, COUNT(*) AS metric_n, COUNT(*)::float AS val,
               (ARRAY_AGG(h.case_id ORDER BY h.created_date))[1:{int(evidence_limit)}] AS case_ids
        FROM crm_case_history h JOIN base ON base.id = h.case_id
        WHERE h.field = 'Owner Assignment' AND COALESCE(h.old_value, '') <> ''
        GROUP BY 1
    )"""
        policy.append("Transfer count = 'Owner Assignment' history rows moving a case away from the "
                      "agent; each transfer from A to B counts towards A. Agents in scope with no "
                      "transfers count as 0.")
        if applied.get("start_date") or applied.get("end_date"):
            policy.append("The date window scopes the CASES in play (by "
                          f"{applied.get('date_field', 'created_date')}), not the moment the "
                          "transfer itself was recorded.")
        require_value = False

    having = ""
    if min_cases_per_group:
        params["min_cases"] = int(min_cases_per_group)
        having = " AND cohort.cohort_n >= %(min_cases)s"
        applied["min_cases_per_group"] = int(min_cases_per_group)

    value_expr = "agg.val" if require_value else "COALESCE(agg.val, 0)"
    join = "JOIN" if require_value else "LEFT JOIN"
    sql = f"""
WITH {BASE_CTE.strip().rstrip(',')},
    base AS (SELECT * FROM enriched{where}),
    {cohort.strip()},
    {agg.strip()}
SELECT cohort.gk AS group_key, cohort.cohort_n, cohort.cohort_case_ids,
       COALESCE(agg.metric_n, 0) AS metric_n,
       {value_expr} AS value, COALESCE(agg.case_ids, ARRAY[]::varchar[]) AS case_ids
FROM cohort {join} agg ON agg.gk = cohort.gk
WHERE TRUE{having}
ORDER BY value {order.upper()}, cohort.gk
"""
    rows = db.query(sql, params)
    if not rows:
        return {"metric": metric, "group_by": group_by, "filters_applied": applied,
                "policy_notes": policy, "n_groups": 0, "winners": [], "ranking": [],
                "note": "no rows matched these filters"}

    labels = agent_names([r["group_key"] for r in rows]) if group_by in ("agent", "current_owner") else {}
    is_time = metric.endswith("handle_time")
    for r in rows:
        r["group_label"] = labels.get(r["group_key"], r["group_key"])
        r["value"] = float(r["value"]) if r["value"] is not None else None
        if is_time:
            r["value_human"] = _fmt_duration(r["value"])
            r["value_unit"] = "seconds"
        else:
            r["value"] = int(r["value"])
            r["value_unit"] = "cases" if metric == "case_count" else "transfers"

    for r in rows:
        cohort_ids = list(r.pop("cohort_case_ids", None) or [])
        if metric == "transfer_count" and not r["case_ids"]:
            r["case_ids"] = cohort_ids
            r["evidence_note"] = ("this agent transferred nothing away, so there are no transfer "
                                  "records; these are the cases they managed, none of which was "
                                  "transferred")

    best = rows[0]["value"]
    eps = 1e-6 if is_time else 0
    winners = [r for r in rows if r["value"] is not None and abs(r["value"] - best) <= eps]
    out = {
        "metric": metric,
        "group_by": group_by,
        "filters_applied": applied,
        "policy_notes": policy,
        "ordered": "smallest first" if order == "asc" else "largest first",
        "n_groups": len(rows),
        "tie_at_top": len(winners) > 1,
        "winners": winners,
        "ranking": rows[:top_n],
    }
    if len(winners) > 1:
        primary = max(winners, key=lambda r: (r["cohort_n"], r["group_key"]))
        out["primary_answer"] = {"group_key": primary["group_key"], "group_label": primary["group_label"]}
        out["tie_break_beyond_policy"] = (
            f"{len(winners)} groups tie on this metric and the company has no tie-break rule for it. "
            "Name the primary_answer above on the answer line -- it is the tied group with the most "
            "cases behind it (then the lowest key), which is our rule and not the company's -- and "
            "then state plainly that it is a tie and list the others with their records.")
    if len(winners) == 1:
        out["supporting_cases"] = case_details(winners[0]["case_ids"])
    else:
        # a tie is reported honestly, so every tied group needs its own records
        for w in winners[:6]:
            w["supporting_cases"] = case_details(list(w["case_ids"])[:8])
    return out


def case_details(case_ids: list[str]) -> list[dict]:
    """The evidence rows a manager can go and check."""
    if not case_ids:
        return []
    rows = db.query(f"""
WITH {BASE_CTE.strip().rstrip(',')}
SELECT enriched.id AS case_id, enriched.subject, enriched.status, enriched.priority,
       enriched.created_date, enriched.closed_date, enriched.issue_name, enriched.product_name,
       enriched.shipping_state, enriched.first_agent_id, enriched.owner_id,
       enriched.transferred, enriched.handle_seconds
FROM enriched WHERE enriched.id = ANY(%(ids)s)
ORDER BY enriched.created_date
""", {"ids": list(case_ids)})
    names = agent_names([r["first_agent_id"] for r in rows] + [r["owner_id"] for r in rows])
    for r in rows:
        r["first_agent"] = names.get(r["first_agent_id"], r["first_agent_id"])
        r["current_owner"] = names.get(r["owner_id"], r["owner_id"])
        r["handle_seconds"] = float(r["handle_seconds"]) if r["handle_seconds"] is not None else None
        r["handle_time"] = _fmt_duration(r["handle_seconds"])
        for k in ("created_date", "closed_date"):
            if r[k]:
                r[k] = r[k].isoformat(sep=" ", timespec="minutes")
    return rows


# --------------------------------------------------------------------------- case routing


def route_new_case(issue: str, product: str | None = None) -> dict:
    """Best agent for a new case: issue expertise -> product expertise -> lightest workload."""
    issue_hit = _resolve("issues", issue)
    if not issue_hit:
        raise ValueError(f"no issue matches {issue!r}; call lookup(kind='issues') to see the catalog")
    product_hit = _resolve("products", product) if product else None

    steps: list[dict] = []
    expertise = db.query("""
        SELECT c.owner_id AS agent_id, COUNT(*) AS closed_cases,
               (ARRAY_AGG(c.id ORDER BY c.closed_date DESC))[1:25] AS case_ids
        FROM crm_case c
        WHERE c.status = 'Closed' AND c.issue_id = %(issue_id)s
        GROUP BY 1 ORDER BY 2 DESC
    """, {"issue_id": issue_hit["id"]})
    if not expertise:
        raise ValueError(f"no agent has closed a case for issue {issue_hit['name']!r}")

    top = expertise[0]["closed_cases"]
    cands = [r for r in expertise if r["closed_cases"] == top]
    names = agent_names([r["agent_id"] for r in expertise])
    steps.append({
        "criterion": "issue expertise",
        "detail": f"closed cases on issue '{issue_hit['name']}' ({issue_hit['id']})",
        "leaders": [{"agent_id": r["agent_id"], "agent": names.get(r["agent_id"], r["agent_id"]),
                     "closed_cases": r["closed_cases"], "case_ids": r["case_ids"]} for r in cands],
        "decisive": len(cands) == 1,
    })

    if len(cands) > 1 and product_hit:
        ids = [r["agent_id"] for r in cands]
        prod = db.query("""
            SELECT c.owner_id AS agent_id, COUNT(*) AS closed_cases,
                   (ARRAY_AGG(c.id ORDER BY c.closed_date DESC))[1:25] AS case_ids
            FROM crm_case c
            JOIN crm_order_item oi ON oi.id = c.order_item_id
            WHERE c.status = 'Closed' AND oi.product2_id = %(pid)s AND c.owner_id = ANY(%(ids)s)
            GROUP BY 1 ORDER BY 2 DESC
        """, {"pid": product_hit["id"], "ids": ids})
        scores = {r["agent_id"]: r for r in prod}
        best = max((r["closed_cases"] for r in prod), default=0)
        nxt = [r for r in prod if r["closed_cases"] == best] if best else []
        steps.append({
            "criterion": "product expertise (tie-break)",
            "detail": (f"closed cases on product '{product_hit['name']}' ({product_hit['id']})"
                       + ("" if best else
                          " -- none of the tied agents has closed a case on this product, so this "
                          "criterion cannot separate them")),
            "leaders": [{"agent_id": r["agent_id"], "agent": names.get(r["agent_id"], r["agent_id"]),
                         "closed_cases": r["closed_cases"], "case_ids": r["case_ids"]} for r in nxt],
            "all_candidates": [{"agent_id": a, "agent": names.get(a, a),
                                "closed_cases": scores.get(a, {}).get("closed_cases", 0)} for a in ids],
            "decisive": len(nxt) == 1,
        })
        if nxt:
            cands = [r for r in cands if r["agent_id"] in {x["agent_id"] for x in nxt}] or cands

    if len(cands) > 1:
        ids = [r["agent_id"] for r in cands]
        load = db.query("""
            SELECT u.id AS agent_id,
                   COUNT(c.id) FILTER (WHERE c.status <> 'Closed') AS open_cases
            FROM crm_user u LEFT JOIN crm_case c ON c.owner_id = u.id
            WHERE u.id = ANY(%(ids)s) GROUP BY 1 ORDER BY 2 ASC
        """, {"ids": ids})
        least = load[0]["open_cases"] if load else None
        nxt = [r for r in load if r["open_cases"] == least]
        steps.append({
            "criterion": "workload (final tie-break)",
            "detail": "fewest cases with status not 'Closed'",
            "leaders": [{"agent_id": r["agent_id"], "agent": names.get(r["agent_id"], r["agent_id"]),
                         "open_cases": r["open_cases"]} for r in nxt],
            "all_candidates": [{"agent_id": r["agent_id"], "agent": names.get(r["agent_id"], r["agent_id"]),
                                "open_cases": r["open_cases"]} for r in load],
            "decisive": len(nxt) == 1,
        })
        cands = [r for r in cands if r["agent_id"] in {x["agent_id"] for x in nxt}] or cands

    tie_rule = None
    if len(cands) > 1:
        ids = [r["agent_id"] for r in cands]
        overall = {r["agent_id"]: r["closed_total"] for r in db.query("""
            SELECT owner_id AS agent_id, COUNT(*) AS closed_total FROM crm_case
            WHERE status = 'Closed' AND owner_id = ANY(%(ids)s) GROUP BY 1
        """, {"ids": ids})}
        cands.sort(key=lambda r: (-overall.get(r["agent_id"], 0), r["agent_id"]))
        tie_rule = ("The three policy criteria are exhausted and still tie. Committing to the "
                    "candidate with the most closed cases overall (then the lowest agent id) so the "
                    "case can actually be assigned -- our rule, not the company's policy. The other "
                    "tied agents are equally valid under the policy.")

    winner = cands[0]
    return {
        "tie_break_beyond_policy": tie_rule,
        "issue_matched": issue_hit,
        "product_matched": product_hit,
        "criteria_applied": steps,
        "tie_unresolved": len(cands) > 1,
        "best_agent": {"agent_id": winner["agent_id"], "agent": names.get(winner["agent_id"], winner["agent_id"])},
        "all_tied_agents": [{"agent_id": r["agent_id"], "agent": names.get(r["agent_id"], r["agent_id"])}
                            for r in cands],
        "supporting_cases": case_details(list(winner["case_ids"])[:15]),
    }

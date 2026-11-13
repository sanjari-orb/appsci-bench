"""Question -> answer, with the arithmetic delegated to analytics.py and the reading to the model."""
from __future__ import annotations

import json
import logging
import re
from datetime import date

import analytics
import dates
import db
import llm

log = logging.getLogger("crm-insights")

MAX_TURNS = 14

#: Case ids in this CRM are 18-character Salesforce ids beginning 500. Used to check that every
#: record the model cites actually came out of a tool, rather than out of the model.
CASE_ID = re.compile(r"\b500[A-Za-z0-9]{12,15}\b")

SYSTEM = """You answer CRM questions for the managers and analysts of a retail company. You have
read-only access to the CRM through tools that do the arithmetic for you.

Two things decide whether an answer is useful, and correctness comes first:
1. CORRECT. Never estimate, never guess, never answer from your own memory. Every number, name and
   identifier in your reply must come from a tool result in THIS conversation.
2. SHOWS ITS WORK. Name the specific case records behind the answer, by case id, so the reader can
   pull them up and verify. An answer with no records behind it is not usable.

# Operational definitions (these override any intuition you have)

HANDLE TIME. The duration taken to close a case: the time from when the case is opened to when it
is closed. Handle time is NOT computed for cases that were transferred to another agent. (A case
never transferred has exactly ONE 'Owner Assignment' history row; a transferred one has MORE THAN
ONE.) The analyze tool already enforces this -- never compute handle time with raw SQL.

MANAGED / HANDLED N CASES. When a question filters on agents who "managed" or "handled" at least N
cases, that count applies to the FIRST agent the case was assigned to. An agent with 2 cases
assigned to them by admin plus 1 transferred in from another agent has managed 2, so a "handled at
least 3 cases" filter excludes them. Pass this as min_cases_per_group.

TRANSFER COUNT. The number of times a case was reassigned from one agent to another. Each transfer
from agent A to agent B counts towards agent A's transfer count.

CASE ROUTING (picking the best agent for a new case), in strict order:
  1. Issue expertise: the agent who has closed the most cases on the issue most similar to the new
     case.
  2. Product expertise: on a tie, the agent who has solved the most cases on the most relevant
     product.
  3. Workload: on a further tie, the agent with the fewest cases whose status is not 'Closed'.

DATE CONVENTIONS.
  Quarters: Q1 = Jan 1-Mar 31, Q2 = Apr 1-Jun 30, Q3 = Jul 1-Sep 30, Q4 = Oct 1-Dec 31, inclusive.
  Seasons: Winter = Dec 1-Feb 28/29, Spring = Mar 1-May 31, Summer = Jun 1-Aug 31,
    Fall/Autumn = Sep 1-Nov 30, inclusive. A season named with a single year means the season
    ENDING in that year, so "Winter 2021" is Dec 1 2020 through Feb 2021.
  "Past/last N months or quarters" with no explicit calendar period is relative to the question's
    own stated "Today's date" (appended to the question text), never to the real current date.
  Always resolve a period with resolve_period rather than doing the arithmetic yourself.

# How to work

- Decide what single thing the question asks for: an agent, a state, a month, an issue, a product.
- Resolve any named or relative period with resolve_period FIRST, then pass start_date/end_date
  into analyze.
- If the question names a product or issue in words, resolve it with lookup before filtering on it.
- Prefer analyze and route_new_case: they apply the policies above. Use sql_query only for
  something they genuinely cannot express, and never to recompute handle time or transfer counts.
- NEVER pass min_cases_per_group unless the question itself states a threshold ("who managed at
  least 3 cases", "agents with 5 or more cases"). If the question states none, there is no minimum:
  a state or agent with a single case is eligible to win, and filtering thin groups out to make the
  result look more sensible produces the WRONG answer. Do not add a threshold on your own judgement,
  and do not mention one that was not asked for.
- Wording -> parameters:
    fastest / quickest / shortest / most efficient  -> avg_handle_time, order='asc'
    slowest / longest                               -> avg_handle_time, order='desc'
    most / top / highest / biggest                  -> case_count, order='desc'
    fewest / least / lowest                         -> case_count, order='asc'
  "Which month/state/issue/product ..." is a case_count question unless it asks about speed of
  closing or resolving, which makes it a handle-time question.
- For "which month" questions: group_by='month' when the window sits inside a single year,
  group_by='month_name' when it spans more than one year (that aggregates the same month across
  years). Either way the answer line names the month, e.g. "March".
- A period filters on when cases were CREATED by default, including on handle-time questions.
  Set date_field='closed' only when the question explicitly ties its window to closing or resolving
  ("among cases closed in Q1 2023"). Leave it unset otherwise.
- Stop as soon as a tool result answers the question. Do not explore further.

# Answer format

Your reply BEGINS with the answer line -- no preamble, no caveat, nothing above it.
Every case id you write MUST appear in a tool result in this conversation. Never adapt, complete or
reconstruct an id from memory -- copy it exactly as the tool returned it.
Line 1, exactly: **Answer: <the thing that was asked for>**
  agent   -> full name then the id in parentheses
  state   -> the 2-letter state code first, the state name after it in parentheses if you like
  month   -> the month name (add the year if the window spans more than one year)
  issue / product -> its name then the id in parentheses
Line 2: the metric value that makes it the answer (a duration in hours, a case count, a transfer
count), and the size of the group it was computed over.
Then "Supporting cases (N):" followed by at most 8 bullets, each a case id plus the figures that
matter for this question.
Then, on one line, "All supporting case ids:" followed by EVERY case id the tool returned for the
answer, comma-separated (say how many more there are if the tool capped the list). The reader needs
the complete set of records the figure rests on, not only the ones you chose to describe.
Then one line starting "Method:" naming the date window actually used and its interpretation, the
policy applied, and any filter.

When the window filters on closed_date, a supporting case can perfectly well have been created
before the window started -- that is expected, not an inconsistency, so do not hedge about it.

TIES. The answer line ALWAYS names exactly one value. Never write "a tie" or a list of names on the
answer line -- a manager asked a question and needs one thing they can act on. When the tool reports
a tie it also returns `primary_answer`: put that on the answer line verbatim. Then, on the very next
line, say plainly that the result is a tie, how many groups tie, and name the others with their
records, repeating the tool's tie_break_beyond_policy note so it is clear which part is company
policy and which part is our choice among equals. Reporting the tie underneath is what keeps this
honest; hiding it is what would not be.

If a tool returns no rows, say the CRM has no matching records; never invent one.
"""

TOOLS = [
    {
        "name": "resolve_period",
        "description": "Turn a period phrase ('Q1 2023', 'Winter 2021', 'the last 3 months', "
                       "'March 2023', '2023') into an inclusive start/end date using the company's "
                       "quarter and season conventions. Pass the question's stated Today's date as "
                       "`today` for anything relative.",
        "input_schema": {
            "type": "object",
            "properties": {
                "phrase": {"type": "string"},
                "today": {"type": "string", "description": "YYYY-MM-DD, the question's own anchor date"},
            },
            "required": ["phrase"],
        },
    },
    {
        "name": "lookup",
        "description": "Resolve or browse a catalog: issues (all 10), products (by name fragment), "
                       "agents (by name or id), states.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["issues", "products", "agents", "states"]},
                "term": {"type": "string"},
            },
            "required": ["kind"],
        },
    },
    {
        "name": "analyze",
        "description": "Rank groups by a metric, with the handle-time and managed-cases policies "
                       "already applied. Returns the ranking, every group tied for first, and the "
                       "case ids behind the winner. This is the main tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string",
                           "enum": ["avg_handle_time", "median_handle_time", "total_handle_time",
                                    "case_count", "transfer_count"]},
                "group_by": {"type": "string",
                             "enum": ["agent", "current_owner", "state", "city", "issue", "product",
                                      "priority", "status", "account", "month", "month_name",
                                      "year", "quarter", "day_of_week"],
                             "description": "'agent' means the FIRST-assigned agent (the policy "
                                            "default); 'month' is YYYY-MM, 'month_name' ignores the year"},
                "order": {"type": "string", "enum": ["asc", "desc"],
                          "description": "asc = smallest first (fastest, fewest)"},
                "min_cases_per_group": {"type": "integer",
                                        "description": "the 'managed/handled at least N cases' filter"},
                "date_field": {"type": "string", "enum": ["created", "closed"],
                               "description": "which date the period filters on. Omit to get the "
                                              "intent default: 'closed' for handle-time metrics, "
                                              "'created' for counts"},
                "filters": {
                    "type": "object",
                    "properties": {
                        "start_date": {"type": "string"},
                        "end_date": {"type": "string"},
                        "issue": {"type": "string", "description": "issue name or id"},
                        "product": {"type": "string", "description": "product name or id"},
                        "state": {"type": "string", "description": "2-letter shipping state code"},
                        "status": {"type": "string"},
                        "priority": {"type": "string"},
                        "agent": {"type": "string", "description": "restrict to one agent (name or id)"},
                    },
                },
                "top_n": {"type": "integer"},
            },
            "required": ["metric", "group_by", "order"],
        },
    },
    {
        "name": "route_new_case",
        "description": "Best agent for a new case under the routing policy (issue expertise, then "
                       "product expertise, then workload). Give the issue the new case is about -- "
                       "pick the closest of the 10 catalog issues -- and the product if the case "
                       "names one.",
        "input_schema": {
            "type": "object",
            "properties": {"issue": {"type": "string"}, "product": {"type": "string"}},
            "required": ["issue"],
        },
    },
    {
        "name": "case_details",
        "description": "Full detail for specific case ids: dates, handle time, issue, product, "
                       "state, first-assigned agent, current owner, whether it was transferred.",
        "input_schema": {
            "type": "object",
            "properties": {"case_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["case_ids"],
        },
    },
    {
        "name": "sql_query",
        "description": "Read-only SELECT against the CRM, for a question analyze cannot express. "
                       "Tables: crm_case(id, priority, subject, description, status, contact_id, "
                       "created_date, closed_date, order_item_id, issue_id, account_id, owner_id), "
                       "crm_account(id, first_name, last_name, person_email, phone, shipping_city, "
                       "shipping_state), crm_user(id, first_name, last_name, email, alias), "
                       "crm_case_history(id, case_id, old_value, new_value, created_date, field), "
                       "crm_issue(id, name), crm_product2(id, name, description, is_active), "
                       "crm_order(id, account_id, ...), crm_order_item(id, order_id, product2_id, "
                       "quantity, unit_price). A case reaches its product via "
                       "order_item_id -> crm_order_item.product2_id -> crm_product2.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    },
]


def _run_tool(name: str, args: dict) -> dict | list:
    if name == "resolve_period":
        today = args.get("today")
        anchor = None
        if today:
            try:
                anchor = date.fromisoformat(str(today)[:10])
            except ValueError:
                anchor = None
        return dates.resolve_period(args["phrase"], anchor)
    if name == "lookup":
        return analytics.lookup(args["kind"], args.get("term"))
    if name == "analyze":
        return analytics.analyze(
            metric=args["metric"], group_by=args["group_by"], filters=args.get("filters"),
            order=args.get("order", "desc"), min_cases_per_group=args.get("min_cases_per_group"),
            date_field=args.get("date_field"), top_n=int(args.get("top_n") or 10))
    if name == "route_new_case":
        return analytics.route_new_case(args["issue"], args.get("product"))
    if name == "case_details":
        return analytics.case_details(list(args.get("case_ids") or []))
    if name == "sql_query":
        return {"rows": db.safe_select(args["sql"])}
    raise ValueError(f"unknown tool {name!r}")


def _strip_unsupported(text: str, bad: set[str]) -> str:
    """Last resort: drop lines citing a record we cannot trace to a tool result, and say so."""
    kept = [ln for ln in text.splitlines() if not any(b in ln for b in bad)]
    kept.append("")
    kept.append("(Note: one or more cited case records could not be verified against the CRM and "
                "have been removed from this answer. Treat the figures above as unconfirmed until "
                "the query is re-run.)")
    return "\n".join(kept)


def _json(obj) -> str:
    return json.dumps(obj, default=str)[:60000]


def answer(question: str) -> str:
    """Answer one question, via the model where available and rules where not."""
    today = dates.extract_today(question)
    prefix = ""
    if today:
        prefix = (f"(The question's own anchor date is {today.isoformat()}; any relative window is "
                  f"relative to it.)\n\n")
    if not llm.available():
        log.warning("no model credentials -- falling back to the rule-based router")
        return fallback_answer(question, today)

    messages: list[dict] = [{"role": "user", "content": prefix + question}]
    trace: list[str] = []
    seen_ids: set[str] = set()
    repaired = False
    try:
        for turn in range(MAX_TURNS):
            reply = llm.complete(messages, system=SYSTEM, tools=TOOLS)
            blocks = reply.get("content", [])
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not uses:
                text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
                if not text:
                    break
                unsupported = set(CASE_ID.findall(text)) - seen_ids
                if unsupported and not repaired:
                    # one chance to correct itself against the tool output it already has
                    repaired = True
                    log.warning("unsupported case ids cited, asking for a rewrite: %s",
                                ", ".join(sorted(unsupported)))
                    messages.append({"role": "assistant", "content": blocks})
                    messages.append({"role": "user", "content": (
                        "These case ids do not appear in any tool result in this conversation: "
                        + ", ".join(sorted(unsupported))
                        + ". Every case id you cite must be copied exactly from a tool result. "
                          "Re-read the tool output and rewrite the answer citing only ids that "
                          "appear there. Keep the same answer and format otherwise.")})
                    continue
                if unsupported:
                    log.error("stripping unverifiable case ids: %s", ", ".join(sorted(unsupported)))
                    text = _strip_unsupported(text, unsupported)
                log.info("answered in %d turn(s); tools used: %s", turn + 1, ", ".join(trace))
                return text
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for use in uses:  # normally exactly one
                trace.append(use.get("name", "?"))
                try:
                    out = _run_tool(use["name"], use.get("input") or {})
                    payload, err = _json(out), False
                    seen_ids.update(CASE_ID.findall(payload))
                except Exception as exc:  # noqa: BLE001 -- hand the model its mistake to fix
                    payload, err = f"ERROR: {type(exc).__name__}: {exc}", True
                    log.warning("tool %s failed: %s", use.get("name"), exc)
                results.append({"type": "tool_result", "tool_use_id": use["id"],
                                "content": payload, "is_error": err})
            messages.append({"role": "user", "content": results})
        log.warning("hit the turn limit without a final answer")
    except llm.LLMUnavailable as exc:
        log.error("model unavailable (%s) -- falling back to the rule-based router", exc)
    return fallback_answer(question, today)


# --------------------------------------------------------------------------- no-model fallback

_PERIOD_RE = re.compile(
    r"(q[1-4]\s*(?:of\s*)?(?:19|20)\d{2}|(?:19|20)\d{2}\s*q[1-4]|"
    r"(?:first|second|third|fourth)\s+quarter\s+(?:of\s+)?(?:19|20)\d{2}|"
    r"(?:winter|spring|summer|fall|autumn)\s+(?:of\s+)?(?:19|20)\d{2}|"
    r"(?:past|last)\s+\w+\s+(?:month|quarter|year|week)s?|"
    r"[a-z]{3,9}\s+(?:19|20)\d{2}|(?:19|20)\d{2})", re.I)

_FAST = re.compile(r"\b(fastest|quickest|shortest|most efficient|least time|speediest)\b", re.I)
_SLOW = re.compile(r"\b(slowest|longest|most time)\b", re.I)
_LEAST = re.compile(r"\b(fewest|least|lowest|minimum|smallest)\b", re.I)


def fallback_answer(question: str, today: date | None = None) -> str:
    """Rule-based router: correct policies, cruder language understanding."""
    q = question.lower()
    filters: dict = {}
    window_note = "all time"
    m = _PERIOD_RE.search(question)
    if m:
        try:
            win = dates.resolve_period(m.group(0), today)
            filters["start_date"], filters["end_date"] = win["start"], win["end"]
            window_note = win["interpretation"]
        except ValueError:
            pass

    for row in analytics.lookup("issues")["matches"]:
        if row["name"].lower() in q:
            filters["issue"] = row["id"]
            break
    m = re.search(r"\b(?:product|item)\s+(?:called\s+)?['\"]?([A-Za-z][A-Za-z0-9' \-]{3,40})['\"]?", question)
    if m:
        hit = analytics._resolve("products", m.group(1).strip())
        if hit:
            filters["product"] = hit["id"]
    m = re.search(r"\b(?:in|from|for)\s+([A-Z]{2})\b", question)
    if m:
        filters["state"] = m.group(1)

    min_cases = None
    m = re.search(r"(?:managed|handled|with)\s+(?:at least\s+)?(\d+)\s+(?:or more\s+)?cases", q)
    if m:
        min_cases = int(m.group(1))

    is_time = bool(_FAST.search(q) or _SLOW.search(q) or "handle time" in q or "resolution time" in q)
    if "transfer" in q:
        metric, group_by = "transfer_count", "agent"
        order = "asc" if _LEAST.search(q) else "desc"
    elif is_time:
        metric = "avg_handle_time"
        order = "asc" if not _SLOW.search(q) else "desc"
        group_by = ("state" if re.search(r"\bstate|region\b", q) else
                    "issue" if "issue" in q else "agent")
    else:
        metric, order = "case_count", ("asc" if _LEAST.search(q) else "desc")
        if re.search(r"\bmonth\b", q):
            group_by = "month" if re.search(r"\b(19|20)\d{2}\b.*\b(19|20)\d{2}\b", q) else "month_name"
        elif re.search(r"\bstate|region\b", q):
            group_by = "state"
        elif re.search(r"\b(complaint|issue|problem)\b", q):
            group_by = "issue"
        elif re.search(r"\bproduct\b", q) and "product" not in filters:
            group_by = "product"
        else:
            group_by = "agent"

    res = analytics.analyze(metric=metric, group_by=group_by, filters=filters, order=order,
                            min_cases_per_group=min_cases)
    return render(res, window_note)


def render(res: dict, window_note: str) -> str:
    """Plain-text rendering of an analyze() result -- answer first, then the records."""
    winners = res.get("winners") or []
    if not winners:
        return ("I could not find any CRM records matching that question, so I have no answer to "
                "give you rather than a guess.")
    lines = []
    if len(winners) > 1:
        lines.append("**Answer: a genuine tie — " +
                     ", ".join(f"{w['group_label']} ({w['group_key']})" for w in winners) + "**")
    else:
        w = winners[0]
        label = w["group_label"]
        key = w["group_key"]
        lines.append(f"**Answer: {label}{f' ({key})' if key != label else ''}**")
    for w in winners:
        val = w.get("value_human") or f"{w['value']} {w.get('value_unit', '')}".strip()
        lines.append(f"- {w['group_label']}: {val}, over {w['metric_n']} case(s) "
                     f"(group total in scope: {w['cohort_n']})")
    ids = winners[0].get("case_ids") or []
    cases = res.get("supporting_cases") or analytics.case_details(ids[:8])
    if cases:
        lines.append("")
        lines.append(f"Supporting cases ({len(ids)} in total, showing {min(8, len(cases))}):")
        for c in cases[:8]:
            bits = [c["case_id"], f"opened {c['created_date']}"]
            if c.get("closed_date"):
                bits.append(f"closed {c['closed_date']}")
            if c.get("handle_time"):
                bits.append(f"handle time {c['handle_time']}")
            bits.append(f"issue {c['issue_name']}")
            lines.append("- " + " | ".join(str(b) for b in bits))
    method = [f"window: {window_note}", f"metric: {res['metric']}", f"grouped by: {res['group_by']}"]
    if res.get("filters_applied"):
        method.append(f"filters: {json.dumps(res['filters_applied'], default=str)}")
    method += res.get("policy_notes", [])
    lines.append("")
    lines.append("Method: " + "; ".join(method))
    return "\n".join(lines)

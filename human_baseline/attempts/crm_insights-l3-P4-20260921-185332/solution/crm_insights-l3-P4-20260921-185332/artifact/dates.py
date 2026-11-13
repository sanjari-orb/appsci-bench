"""Date-window conventions the questions are phrased in (elicited from the stakeholder).

Quarters   Q1 Jan1-Mar31, Q2 Apr1-Jun30, Q3 Jul1-Sep30, Q4 Oct1-Dec31 (inclusive).
Seasons    Winter Dec1-Feb28/29, Spring Mar1-May31, Summer Jun1-Aug31, Fall Sep1-Nov30
           (inclusive). A season named with one year means the season *ending* in that year, so
           "Winter 2021" = 2020-12-01 .. 2021-02-28.
Relative   "past/last N months|quarters" is relative to the question's own stated "Today's date",
           which is appended to the question text -- never to the real current date.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

QUARTERS = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}
ORDINAL_Q = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
SEASONS = {
    "winter": None,  # special-cased: spans the year boundary
    "spring": (3, 1, 5, 31),
    "summer": (6, 1, 8, 31),
    "fall": (9, 1, 11, 30),
    "autumn": (9, 1, 11, 30),
}


def _eom(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def month_window(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, _eom(year, month))


def shift_months(d: date, months: int) -> date:
    """Shift a date by whole months, clamping the day to the target month's length."""
    total = (d.year * 12 + d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, _eom(year, month)))


# ------------------------------------------------------------------ today's anchor

_TODAY_PATTERNS = (
    r"today'?s?\s+date\s*(?:is|:)?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
    r"today\s*(?:is|:)\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
    r"today'?s?\s+date\s*(?:is|:)?\s*([A-Za-z]+\s+[0-9]{1,2},?\s+[0-9]{4})",
    r"current\s+date\s*(?:is|:)?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
)


def extract_today(text: str) -> date | None:
    """Pull the question's own anchor date out of the question text."""
    for pat in _TODAY_PATTERNS:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        raw = m.group(1).strip().rstrip(".")
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
        m2 = re.match(r"([A-Za-z]+)\s+([0-9]{1,2}),?\s+([0-9]{4})", raw)
        if m2 and m2.group(1).lower() in MONTHS:
            return date(int(m2.group(3)), MONTHS[m2.group(1).lower()], int(m2.group(2)))
    return None


# ------------------------------------------------------------------ named windows


def resolve_period(phrase: str, today: date | None = None) -> dict:
    """Turn a period phrase into {start, end, interpretation}; raise ValueError if unrecognised."""
    p = " ".join(phrase.lower().split())

    # explicit ISO range
    m = re.search(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\s*(?:to|through|-|and|until)\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", p)
    if m:
        return _out(date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2)), "explicit date range")

    # quarter: "q1 2023" / "2023 q1" / "first quarter of 2023"
    m = re.search(r"\bq([1-4])\b[^0-9]{0,12}((?:19|20)[0-9]{2})", p) or re.search(
        r"((?:19|20)[0-9]{2})[^0-9]{0,12}\bq([1-4])\b", p)
    if m:
        a, b = m.group(1), m.group(2)
        q, year = (int(a), int(b)) if len(a) == 1 else (int(b), int(a))
        return _quarter(q, year)
    m = re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\b\D{0,8}((?:19|20)[0-9]{2})", p)
    if m:
        return _quarter(ORDINAL_Q[m.group(1)], int(m.group(2)))

    # season
    m = re.search(r"\b(winter|spring|summer|fall|autumn)\b\D{0,8}((?:19|20)[0-9]{2})", p)
    if m:
        return _season(m.group(1), int(m.group(2)))

    # "march 2023"
    m = re.search(r"\b([a-z]{3,9})\s+((?:19|20)[0-9]{2})\b", p)
    if m and m.group(1) in MONTHS:
        year, month = int(m.group(2)), MONTHS[m.group(1)]
        s, e = month_window(year, month)
        return _out(s, e, f"calendar month {calendar.month_name[month]} {year}")

    # relative to the question's anchor date
    m = re.search(r"\b(?:past|last|previous|preceding|trailing)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve)?\s*(month|quarter|year|week|day)s?\b", p)
    if m:
        if today is None:
            raise ValueError("relative window needs the question's stated Today's date")
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
        raw_n = m.group(1)
        n = int(raw_n) if (raw_n or "").isdigit() else words.get(raw_n or "", 1)
        unit = m.group(2)
        if unit == "month":
            start = shift_months(today, -n)
        elif unit == "quarter":
            start = shift_months(today, -3 * n)
        elif unit == "year":
            start = shift_months(today, -12 * n)
        elif unit == "week":
            start = date.fromordinal(today.toordinal() - 7 * n)
        else:
            start = date.fromordinal(today.toordinal() - n)
        return _out(start, today,
                    f"rolling {n} {unit}(s) back from the question's stated today ({today.isoformat()})")

    # bare year
    m = re.search(r"\b((?:19|20)[0-9]{2})\b", p)
    if m:
        year = int(m.group(1))
        return _out(date(year, 1, 1), date(year, 12, 31), f"calendar year {year}")

    raise ValueError(f"could not resolve a date window from {phrase!r}")


def _quarter(q: int, year: int) -> dict:
    m1, m2 = QUARTERS[q]
    return _out(date(year, m1, 1), date(year, m2, _eom(year, m2)), f"Q{q} {year}")


def _season(name: str, year: int) -> dict:
    if name == "winter":
        return _out(date(year - 1, 12, 1), date(year, 2, _eom(year, 2)),
                    f"Winter ending in {year} (Dec 1 {year - 1} - Feb {_eom(year, 2)} {year})")
    m1, d1, m2, d2 = SEASONS[name]
    return _out(date(year, m1, d1), date(year, m2, d2), f"{name.title()} {year}")


def _out(start: date, end: date, interpretation: str) -> dict:
    return {"start": start.isoformat(), "end": end.isoformat(), "interpretation": interpretation}

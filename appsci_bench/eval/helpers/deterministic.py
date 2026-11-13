"""Deterministic comparison helpers.

Four building blocks, deliberately not more: strict string equality and naive numeric-difference
"similarity" are one-liners every task designer can write themselves and don't belong in a shared
library. These are the ones where getting the comparison subtly wrong is easy and common:

  resolve_scalar     -- smart single-value resolution (numeric-with-units, boolean aliases,
                        normalized-string, explicit "incomparable" fallback).
  parse_bool          -- single-value boolean normalization (the alias table resolve_scalar uses
                        internally, exported for tasks that need to coerce one raw value rather
                        than compare two).
  text_similarity     -- continuous [0,1] free-text similarity (no LLM).
  json_field_match    -- per-field JSON equivalence composed from the two above, with an
                        auto-detected-or-overridable rule per field.

`make_json_field_eval_function` is factory sugar: wraps a single-field `json_field_match` call as
a ready `eval_function_path` target for the common single-field case.
"""
from __future__ import annotations

import difflib
import json
import math
import re
from typing import Any, Callable, Literal

FieldRule = Literal["scalar", "text", "set"]

_BOOL_ALIASES: dict[str, bool] = {
    "true": True, "false": False,
    "yes": True, "no": False,
    "flag": True, "flagged": True, "unflagged": False,
    "1": True, "0": False,
}

# Strip thousands separators / currency / percent signs before trying to parse a number, then allow
# a trailing unit word ("16 units", "16kg") -- but not embedded ones ("16.5.2" stays unparseable).
_NUMERIC_STRIP_RE = re.compile(r"[,$%]")
_NUMERIC_WITH_UNIT_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*[a-zA-Z]*\s*$")

# Tokenizer for text_similarity's token_set_ratio: split on anything that isn't a word character.
_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)


def _try_parse_number(value: Any) -> float | None:
    """Parse `value` as a number, tolerating common formatting. None if it doesn't parse.

    Bools are deliberately excluded here (isinstance(True, int) is True in Python) -- they go
    through the boolean path in `resolve_scalar`, never silently compared as 0.0/1.0.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = _NUMERIC_STRIP_RE.sub("", value.strip())
    if not s:
        return None
    m = _NUMERIC_WITH_UNIT_RE.match(s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_bool(value: Any) -> bool | None:
    """Parse `value` as a boolean via a small alias table. None if it doesn't resolve.

    Exported (not just resolve_scalar-internal) because normalizing a single stringly-typed flag
    ("false"/"no"/"0") is its own common need -- a naive `bool(value)` is always True for any
    non-empty string, silently misreading e.g. `{"flagged": "false"}` as flagged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        alias = _BOOL_ALIASES.get(value.strip().lower())
        if alias is not None:
            return alias
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def resolve_scalar(
    predicted: Any, expected: Any, *,
    numeric_rel_tol: float = 1e-3, numeric_abs_tol: float = 1e-6,
    case_sensitive: bool = False,
) -> tuple[bool, str]:
    """Smart single-value comparison. Returns (matched, how).

    Resolution order (first that applies to BOTH values wins; falls through otherwise):
      1. numeric (after stripping units/currency/thousands-separators), tolerance-compared;
      2. boolean/enum-ish (true/false, yes/no, flag/flagged/unflagged, 1/0, case-insensitive);
      3. exact-after-normalization string (casefold + strip, unless case_sensitive=True);
      4. "incomparable" -- NOT a silent string-equality attempt on mismatched types. Callers that
         want a fuzzy fallback compose explicitly with `text_similarity`.
    """
    p_num, e_num = _try_parse_number(predicted), _try_parse_number(expected)
    if p_num is not None and e_num is not None:
        matched = math.isclose(p_num, e_num, rel_tol=numeric_rel_tol, abs_tol=numeric_abs_tol)
        return matched, "numeric"

    p_bool, e_bool = parse_bool(predicted), parse_bool(expected)
    if p_bool is not None and e_bool is not None:
        return p_bool == e_bool, "boolean"

    if isinstance(predicted, str) and isinstance(expected, str):
        p_s, e_s = predicted.strip(), expected.strip()
        if not case_sensitive:
            p_s, e_s = p_s.casefold(), e_s.casefold()
        return p_s == e_s, "normalized_string"

    return False, "incomparable"


def text_similarity(predicted: str, expected: str, *, method: str = "token_set_ratio") -> float:
    """Continuous [0,1] free-text similarity. Not a boolean -- callers pick their own threshold.

      token_set_ratio (default): casefold, tokenize on non-word characters, compare as SETS
        (order- and duplicate-insensitive) via a Jaccard ratio. Robust to reordering.
      sequence_ratio: difflib.SequenceMatcher ratio -- order-sensitive, for when phrasing order
        actually matters.

    Stdlib-only (difflib) so this adds no dependency.
    """
    if method == "token_set_ratio":
        p_tokens = {t for t in _TOKEN_RE.split(predicted.casefold()) if t}
        e_tokens = {t for t in _TOKEN_RE.split(expected.casefold()) if t}
        if not p_tokens and not e_tokens:
            return 1.0
        if not p_tokens or not e_tokens:
            return 0.0
        return len(p_tokens & e_tokens) / len(p_tokens | e_tokens)
    if method == "sequence_ratio":
        return difflib.SequenceMatcher(None, predicted, expected).ratio()
    raise ValueError(f"unknown text_similarity method: {method!r}")


def _sets_match(predicted: list[Any], expected: list[Any]) -> bool:
    """Order-insensitive set equality via `resolve_scalar` per element (bipartite, greedy)."""
    if len(predicted) != len(expected):
        return False
    remaining = list(predicted)
    for e in expected:
        for i, p in enumerate(remaining):
            if resolve_scalar(p, e)[0]:
                del remaining[i]
                break
        else:
            return False
    return True


def _match_field(
    predicted: Any, expected: Any, explicit_rule: FieldRule | None, text_threshold: float,
) -> tuple[bool, str]:
    """Resolve one field's rule (explicit override, else auto-detect from `expected`'s type)."""
    if explicit_rule == "set" or (explicit_rule is None and isinstance(expected, (list, tuple, set))):
        exp_list = list(expected) if isinstance(expected, (list, tuple, set)) else [expected]
        pred_list = list(predicted) if isinstance(predicted, (list, tuple, set)) else [predicted]
        return _sets_match(pred_list, exp_list), "set"

    if explicit_rule == "text":
        sim = text_similarity(str(predicted), str(expected))
        return sim >= text_threshold, f"text_similarity={sim:.3f}"

    matched, how = resolve_scalar(predicted, expected)
    # Auto-detected (no explicit override) string fields that fail an exact normalized-string
    # compare fall back to text_similarity -- NOT gated on how == "incomparable": two plain strings
    # always resolve via the "normalized_string" path in resolve_scalar (matched or not), so
    # "incomparable" never actually occurs for a str/str pair. Gating on "normalized_string"
    # specifically (rather than "not matched" generally) also keeps this from misfiring on a
    # genuine numeric-string mismatch (e.g. "100" vs "105"), which resolves via the "numeric" path
    # and should stay a numeric miss, not get a fuzzy-text second chance.
    if not matched and how == "normalized_string" and explicit_rule is None:
        sim = text_similarity(str(predicted), str(expected))
        return sim >= text_threshold, f"text_similarity_fallback={sim:.3f}"
    return matched, how


def json_field_match(
    predicted: dict, expected: dict, *,
    field_rules: dict[str, FieldRule] | None = None,
    required_fields: list[str] | None = None,
    text_similarity_threshold: float = 0.85,
) -> tuple[float, dict]:
    """Smart per-field JSON equivalence. Returns (score in [0,1] = fraction matched, metadata).

    Compares `expected`'s fields (or `required_fields`, if given, so extra predicted keys don't
    silently count against the task) against `predicted`. Per field, unless overridden in
    `field_rules`: bool/int/float/str use `resolve_scalar` (str falls back to `text_similarity`
    when the exact normalized-string compare fails); list/set/tuple use an order-insensitive
    set-compare.

    A field missing from `predicted` scores that field 0 without raising -- `metadata["per_field"]`
    always lists which fields matched, didn't, or were missing, so a partial score is legible.

    CALLER GUARD, not a bug here: with nothing to compare (an empty `expected` and no
    `required_fields`) this returns a vacuous 1.0 -- correct for a pure comparison, dangerous for a
    scorer. An eval function must therefore reject a missing oracle and a missing payload BEFORE
    calling this, rather than letting either collapse to `{}`; see `tasks/_TEMPLATE/eval.py`'s
    `field_match` for the pattern. `make_json_field_eval_function` below already does both.
    """
    field_rules = field_rules or {}
    fields = required_fields if required_fields is not None else list(expected.keys())
    if not fields:
        return 1.0, {"per_field": {}}

    per_field: dict[str, dict] = {}
    matched_count = 0
    for field in fields:
        if field not in predicted:
            per_field[field] = {"matched": False, "how": "missing_in_predicted"}
            continue
        matched, how = _match_field(
            predicted[field], expected.get(field), field_rules.get(field), text_similarity_threshold)
        per_field[field] = {"matched": matched, "how": how}
        if matched:
            matched_count += 1

    return matched_count / len(fields), {"per_field": per_field}


def make_json_field_eval_function(
    field: str, *, rule: FieldRule = "scalar",
) -> Callable[..., tuple[float, dict]]:
    """Factory sugar: a ready eval_function_path target comparing one field of `.json`.

    For the common single-field case ("flagged", "fault_type") so a task's eval.py can write
    ``flag_accuracy = make_json_field_eval_function("flagged")`` instead of hand-rolling the
    keyword-only wrapper. Reads the agent's actual output from the LAST turn of `conversation`
    (the eval-function contract's `conversation` list) and the ground truth from
    `expected_output.json`; both must be JSON objects, per the design's json-string convention.
    """

    def eval_function(
        *, data_point_id: str, conversation: list, expected_output=None, eval_rubric: str | None = None,
        **kwargs: Any,
    ) -> tuple[float, dict]:
        del data_point_id, eval_rubric, kwargs  # unused -- part of the keyword-only contract
        if expected_output is None or expected_output.json is None:
            return 0.0, {"error": "no expected_output.json to compare against"}
        if not conversation or conversation[-1].response.json is None:
            return 0.0, {"error": "no response.json to compare against"}
        expected = json.loads(expected_output.json)
        actual = json.loads(conversation[-1].response.json)
        return json_field_match(actual, expected, field_rules={field: rule}, required_fields=[field])

    eval_function.__name__ = f"json_field_{field}"
    return eval_function

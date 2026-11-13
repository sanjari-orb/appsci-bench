"""Generic LLM-judge call -- Level 0 of the judge composition.

`llm_judge_call` is the base every higher judge level (transcript, file-similarity) is built on: a
structured-output chat call (re-sampled on an unreadable response, see `JUDGE_PARSE_ATTEMPTS`), reusing
the exact transport `appsci_bench.appsci_agent_user_sim` already uses
(`appsci_bench.common.model_config`'s `ModelConfig`/`complete`) and generalizing its parse-with-fallback
pattern (`usersim_core.py`'s `_parse_message`/`_first_json_object`, written for one hardcoded schema)
to any pydantic `BaseModel`.

This module does NOT reach into `appsci_agent_user_sim`'s internals -- it only imports the shared
`appsci_bench.common` transport, matching the decoupling boundary documented on that package.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError

from appsci_bench.common.model_config import ModelConfig, complete, config_for

logger = logging.getLogger(__name__)

#: Model calls allowed for one judgment when the output cannot be PARSED.
#:
#: A parse failure is not a transport failure, and the fix is a different one. The request succeeded;
#: the model simply answered in a shape `_parse_structured` cannot read, so what it needs is a FRESH
#: SAMPLE rather than time. Re-sampling works because no temperature is pinned anywhere in this stack --
#: `common/model_config` deliberately leaves it at the provider default -- so two calls with identical
#: messages are two different samples. Were a temperature of 0 ever set, this retry would return the
#: same unparseable text three times and should be revisited.
#:
#: NO BACKOFF BETWEEN ATTEMPTS, which is where this deliberately differs from the elicitation judge's
#: own retry (`eval/elicitation.py`, waits of 2s then 6s). That one sits under a fan-out with no other
#: deadline. These calls run inside `score_records`, where every (data point, objective) pair is bounded
#: by a per-eval-function ceiling -- 180s on every orchestrator path
#: (`BenchmarkOrchestrator.EVAL_TIMEOUT_S`, raised from the module default precisely because 60s was
#: timing out gateway judges and scoring them 0), and `DEFAULT_EVAL_TIMEOUT_S` (60s) for a direct caller
#: that leaves it alone. Eight seconds of sleeping inside the tighter of those would convert a
#: recoverable parse failure into a timeout, which is a worse outcome rather than a better one. The
#: failure mode waiting actually helps with, a rate-limit burst, is already backed off inside
#: `complete()`.
#:
#: WHY IT MATTERS AT ALL: without it, one unreadable response scores that case 0 like a wrong answer.
#: Measured across the eval reports on disk, that happens to 0.4%-3% of cases, which is a downward bias
#: on every score for a reason that has nothing to do with what is being judged.
#:
#: INTERACTION WITH THE ELICITATION JUDGE, which calls this and retries on top of it. A hopeless item
#: now costs up to three inner attempts inside each of three outer ones. That is bounded by count, but
#: NOT by time in the way one might assume: all three inner attempts share ONE `ITEM_TIMEOUT_S` window
#: (120s), because that judge bounds the whole item with a single `_call_with_timeout`, and one
#: `complete()` alone is configured for a 120s transport timeout with its own retries -- so a single call
#: can consume the entire item budget and three do not "fit". The consequence, stated honestly: net
#: judgment chances per item go UP, while an item that fails to parse twice may have its third attempt
#: truncated by the outer bound and recorded as a timeout rather than a parse failure. That trade is
#: still worth it, because in the COMMON case this is an improvement rather than a cost -- a single parse
#: flake is now fixed here, so that path no longer pays the outer 2s/6s waits for it at all. Its outer
#: retry stays as it is: it also covers transport and timeout, which this does not.
#:
#: Three attempts, matching the elicitation judge's count.
JUDGE_PARSE_ATTEMPTS = 3

_SchemaT = TypeVar("_SchemaT", bound=BaseModel)


class JudgeParseError(RuntimeError):
    """Raised when the model's output cannot be parsed into `output_schema` (fail loud, no default)."""


class JudgeVerdict(BaseModel):
    """The default judge output shape: reasoning BEFORE score, so the model reasons before scoring.

    Field order matters here -- pydantic/JSON-mode models tend to fill fields in the order they're
    declared, so `reasoning` first is a soft nudge toward chain-of-thought before the number. Callers
    needing more structure (e.g. a per-dimension breakdown) can pass their own `output_schema`
    subclass/sibling as long as it keeps a `score: float` field -- `llm_judge_call` doesn't require
    inheriting from `JudgeVerdict` specifically, just having that field.

    `model_config = {"extra": "forbid"}`: pydantic's default is to silently DROP any JSON field not
    declared on the model. That's the wrong default for a judge -- if the model hallucinates an extra
    field, or a prompt/schema edit introduces a name mismatch (e.g. we ask for "confidence" but the
    model writes "confidence_score"), silently dropping it hides a real prompt/schema drift as a
    clean-looking parse. "forbid" turns that into a `ValidationError` instead (caught by
    `_parse_structured`'s candidate loop, ultimately surfacing as a `JudgeParseError` if no candidate
    validates) -- a loud, debuggable failure rather than a judge that's quietly ignoring output.
    """
    model_config = {"extra": "forbid"}
    reasoning: str
    score: float = Field(ge=0.0, le=1.0)


# A chat message's `content` may be a plain string OR a list of typed content blocks (text +
# document/image), per the OpenAI/Anthropic/LiteLLM multimodal message format -- needed so Level 2
# (the file-similarity judge) can pass native document/image blocks through
# `llm_judge_call` unchanged, rather than this module forcing everything through a string.
PromptContent = str | list[dict[str, Any]]


def llm_judge_call(
    *, system_prompt: str, user_prompt: PromptContent,
    output_schema: type[_SchemaT] = JudgeVerdict,
    config: ModelConfig | None = None,
    caller: str = "judge",
    source: Any = None,
) -> _SchemaT:
    """Make a structured-output chat call and parse it into `output_schema`.

    Re-samples up to `JUDGE_PARSE_ATTEMPTS` times when the response cannot be PARSED -- see that
    constant for why re-sampling is the right fix and why there is no delay between attempts. A
    transport failure is not retried here; `complete()` owns that, with backoff.

    `user_prompt` is usually plain text, but MAY be a list of content blocks instead (e.g.
    `[{"type": "text", "text": "..."}, {"type": "document", "source": {...}}]`) -- passed straight
    through to `complete()`/LiteLLM unmodified, so a caller building a multimodal judge (native
    PDF/image input, the file-similarity judge's "guard, not limit" default) doesn't need a second
    entry point. `system_prompt` stays plain text -- there's no current need for a multimodal system
    turn.

    `system_prompt` gets `_describe_output_schema(output_schema)` appended automatically before the
    call -- an explicit, generated list of `output_schema`'s JSON keys/types/required-ness. This is
    NOT optional prompt-engineering advice: a caller's own prose ("...matching the requested
    schema") is not enough on its own, since some field-name conventions (`JudgeVerdict`'s
    reasoning/score) read as obvious to a model even unstated, while others don't, and a model can
    drop a required field with zero warning otherwise -- `_parse_structured` raises
    `JudgeParseError` rather than guessing when that happens, but the better fix is not needing
    that fallback in the first place. Generating this from the schema itself means every caller
    gets it for free and it can never drift out of sync with the schema the way hand-written prose
    can -- a caller's own system prompt should describe ROLE/BEHAVIOR, not re-enumerate JSON keys
    the schema already defines.

    Requests JSON-object output (`complete(..., json_object=True)`) as a best-effort hint, then
    parses defensively regardless of whether the provider actually honored it: (1) strict JSON
    validate; (2) strip a ```json ... ``` fence and retry; (3) extract the first balanced {...}
    block and validate; (4) as a last resort, salvage the schema's fields from prose that never
    produced an object at all. Those are strategies applied to ONE response; if all of them fail the
    call is re-sampled, and only when every attempt fails does `JudgeParseError` propagate. Never
    default-fills a score on failure -- the caller (an eval function) lets that propagate so the
    orchestrator's catch-all failure boundary turns it into score 0.0 + a recorded error, which is the
    honest outcome, not a silently-wrong judgment.

    `caller` and `source` are observability labels only and change nothing about the request. They
    exist because this helper has one non-judge user -- the eval-time artifact user sim -- and the
    model-call store is sharded by source: left at the default, that sim's calls are filed as the
    evaluator's and every usage report adds them to the judges' line.

    An out-of-range or non-numeric `score` is treated the same as a parse failure (see
    `_parse_structured`) -- we never clamp or rescale a judge's number to force it into [0, 1],
    since guessing the model's intended scale (e.g. "it must mean out of 10") would be a silent,
    unverifiable assumption. `JudgeVerdict.score` itself is `Field`-constrained to [0, 1] so this is
    enforced by construction for the default schema; a custom `output_schema` that redeclares
    `score: float` without the same constraint is still caught by the generic runtime check in
    `_parse_structured` (belt-and-suspenders: the Field constraint documents the intent on the type,
    the runtime check guarantees it regardless of what the caller's schema declares).
    """
    cfg = config or config_for("JUDGE_MODEL")
    full_system_prompt = f"{system_prompt}\n\n{_describe_output_schema(output_schema)}"
    messages = [
        {"role": "system", "content": full_system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_error: JudgeParseError | None = None
    for attempt in range(1, JUDGE_PARSE_ATTEMPTS + 1):
        # Outside the try on purpose: a transport failure must propagate rather than be re-sampled as
        # though it were a parse failure. `complete()` already owns transport retries.
        raw = complete(messages, cfg, json_object=True, caller=caller, source=source)
        try:
            return _parse_structured(raw, output_schema)
        except JudgeParseError as exc:
            last_error = exc
            logger.info(
                "judge output unparseable on attempt %d of %d (%s); re-sampling",
                attempt, JUDGE_PARSE_ATTEMPTS, exc,
            )
    # The attempt count belongs in the message: it lands in the eval report, and a reader needs to know
    # this was not a one-off flake before treating the resulting 0 as anything.
    raise JudgeParseError(
        f"{last_error} (unparseable on all {JUDGE_PARSE_ATTEMPTS} attempts)"
    ) from last_error


def _describe_output_schema(schema: type[BaseModel]) -> str:
    """Render `schema`'s JSON shape as an explicit, model-facing key list: name, type, and
    required/optional, plus any per-field `Field(description=...)` hint.

    Built ONLY from `model_json_schema()`'s `properties`/`required` -- deliberately never includes
    the schema's own class-level `description` (the class docstring). That docstring is internal
    developer documentation (implementation rationale, cross-references to other modules), often
    long, and not written for a model to read; sending it as part of the prompt would be noise at
    best and confusing at worst.

    A cross-field validation rule (e.g. `_SimTurn`'s "message required unless stopped is true")
    cannot be expressed here -- JSON Schema's `required` list is static/unconditional, so that kind
    of business rule stays the calling module's own responsibility to state in its `system_prompt`.
    This function only ever guarantees the KEYS and their SHAPE, not conditional relationships
    between them.
    """
    spec = schema.model_json_schema()
    required = set(spec.get("required", []))
    defs = spec.get("$defs", {})
    lines = []
    for name, prop in spec.get("properties", {}).items():
        status = "required" if name in required else "optional"
        hint = f" -- {prop['description']}" if prop.get("description") else ""
        lines.append(f'  "{name}" ({_json_type_str(prop, defs)}, {status}){hint}')
    return ("Respond with a single JSON object with EXACTLY these keys and no others:\n"
            + "\n".join(lines))


def _json_type_str(prop: dict, defs: dict | None = None, _depth: int = 0) -> str:
    """A short, model-facing type description for one `properties` entry from a JSON Schema.

    Handles the shapes this codebase's schemas actually use (string/number/integer/boolean, array,
    nullable-via-anyOf, enum, and a NESTED OBJECT via `$ref`/inline `properties`) -- not a general
    JSON-Schema-to-English renderer. Falls back to "value" for anything else rather than dumping the
    raw schema fragment, since an unrecognized shape is still better described vaguely than not at all.

    A nested object is rendered with its OWN keys, not as a bare type word, because a model cannot
    emit an object whose shape it was never shown. It guesses instead, and the guess is not a near
    miss: asked for an optional tool call described only as "value", the eval-time user simulator
    wrote the tool's NAME as a bare string (`"call_user_tool": "call_discoverable_user_tool"`) in 125
    of 125 observed failures. Every one failed validation, and because a turn that will not parse
    aborts the case, that cost 25 of 97 cases in run banking_support_20260818_184400 -- scored as
    zeros across all five objectives, for a formatting slip by our own instrument. `defs` carries
    `model_json_schema()`'s `$defs` so a `$ref` can be resolved; `_depth` stops a self-referential
    schema from recursing forever, and one level of nesting is all any caller's schema needs today.
    """
    if "enum" in prop:
        return "one of " + ", ".join(repr(v) for v in prop["enum"])
    if "$ref" in prop:
        return _object_type_str(_resolve_ref(prop["$ref"], defs), defs, _depth)
    if "type" in prop:
        t = prop["type"]
        if t == "array":
            return f"array of {_json_type_str(prop.get('items', {}), defs, _depth)}"
        if t == "object" and prop.get("properties"):
            return _object_type_str(prop, defs, _depth)
        return t
    if "anyOf" in prop:
        non_null = [p for p in prop["anyOf"] if p.get("type") != "null"]
        if non_null:
            return _json_type_str(non_null[0], defs, _depth)
    return "value"


def _resolve_ref(ref: str, defs: dict | None) -> dict:
    """Look up a local `#/$defs/<name>` reference. Unresolvable -> {} (renders as "value")."""
    prefix = "#/$defs/"
    if not ref.startswith(prefix) or not defs:
        return {}
    return defs.get(ref[len(prefix):], {}) or {}


def _object_type_str(spec: dict, defs: dict | None, _depth: int) -> str:
    """Render a nested object as `object with keys {"k": <type> (required), ...}`.

    Past one level of nesting this degrades to the bare word "object": deeper shapes are not something
    any current caller's schema has, and an unbounded renderer would put a wall of text in a prompt
    where a template is what helps.
    """
    props = spec.get("properties") or {}
    if _depth >= 1 or not props:
        return "object"
    required = set(spec.get("required", []))
    parts = [
        f'"{k}": {_json_type_str(v, defs, _depth + 1)} '
        f'({"required" if k in required else "optional"})'
        for k, v in props.items()
    ]
    return "object with keys {" + ", ".join(parts) + "}"


def _parse_structured(raw: str, schema: type[_SchemaT]) -> _SchemaT:
    """Parse `raw` into `schema`, tolerating a fenced code block or prose around the JSON object.

    Generalizes `usersim_core.py`'s `_parse_message`/`_first_json_object` (written for one hardcoded
    `_SimulatorTurn` schema) to any pydantic `BaseModel`. A candidate that parses but carries a
    `score` outside [0, 1] (or a non-numeric one) is treated as a failed candidate too, not returned
    -- this generic check is what protects a custom `output_schema` that forgot to add its own
    `Field(ge=0.0, le=1.0)` constraint (see `llm_judge_call`'s docstring).

    LAST RESORT (`_salvage_fields`): a judge that writes its reasoning as prose and then closes with
    `..., "score": 0.9}` produces no parseable object at all -- the opening brace never arrives. That
    was 5 of 60 samples in one real order_to_cash run, each becoming a silent 0.0 for a metric whose
    judgment was fine, which drags a weighted score down for a formatting slip by OUR OWN instrument.

    Being lenient here is the opposite of the strictness the artifact is held to, and deliberately so:
    the artifact's response shape is the thing under measurement, so repairing it would erase the
    signal. The judge is the measuring device, not the subject -- its formatting carries no signal, so
    reading a value it stated plainly is recovering data, not inventing it. The salvage never supplies
    a value the model did not write, and anything it does produce still goes through the same schema
    validation and range check below.
    """
    candidates: list[str] = [raw]

    fenced = re.search(r"```(?:json)?\s*(\{[^}]*\})\s*```", raw, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))

    block = _first_json_object(raw)
    if block is not None:
        candidates.append(block)

    salvaged = _salvage_fields(raw, schema)
    if salvaged is not None:
        candidates.append(salvaged)

    for candidate in candidates:
        try:
            parsed = schema.model_validate_json(candidate)
        except ValidationError:
            continue
        score = getattr(parsed, "score", None)
        is_valid_score = (
            score is None  # schema has no `score` field at all -- not this function's concern
            or (isinstance(score, (int, float)) and not isinstance(score, bool)
                and 0.0 <= score <= 1.0)
        )
        if is_valid_score:
            return parsed
        # else: fall through to the next candidate, same as a ValidationError.

    raise JudgeParseError(
        f"could not parse a {schema.__name__} verdict from model output: {raw!r}")


def _salvage_fields(raw: str, schema: type[BaseModel]) -> str | None:
    """Rebuild a JSON object from `"key": value` pairs stated in `raw`, or None if it can't be done.

    For output that never became an object -- the shape observed in practice is prose, then a trailing
    `..., "score": 0.9}` whose opening brace never arrived. See `_parse_structured` for why recovering
    the judge's own stated value is legitimate where repairing the ARTIFACT's would not be.

    Deliberately narrow, so it can only ever read back what the model wrote:
      - only keys the schema declares are looked for, so stray numbers in the prose are ignored;
      - three written forms are recognised, all observed from real judges: a quoted JSON pair
        (`"score": 0.9`), an XML-ish tag (`<score>0.9</score>`), and a line-leading unquoted pair
        (`score: 0.9`). The unquoted form is anchored to the start of a line so prose such as
        "...warranting a score: high" cannot match;
      - the value must be a JSON scalar (number, quoted string, bool, null) that `json.loads` accepts
        as-is -- no coercion, no unit-stripping, no scale-guessing;
      - the LAST occurrence of a key wins, since a judge that restates its verdict does so at the end;
      - every REQUIRED field must be found, else None -- a partial object would be a fabricated verdict.
    Free-text fields (a `reasoning` written as bare prose rather than a quoted value) are filled from
    the whole raw output, since that prose IS the reasoning; that only ever affects metadata, never a
    score. Whatever this returns still faces the schema and the [0, 1] range check.
    """
    spec = schema.model_json_schema()
    props: dict = spec.get("properties", {})
    if not props:
        return None
    required = list(spec.get("required", []))
    scalar = r'-?\d+\.?\d*|"[^"]*"|true|false|null'

    out: dict[str, Any] = {}
    for name in props:
        key = re.escape(name)
        for pattern in (
            rf'"{key}"\s*:\s*({scalar})',          # a real JSON pair, brace or no brace
            rf'<{key}>\s*({scalar})\s*</{key}>',   # XML-ish tags
            rf'(?m)^\s*{key}\s*:\s*({scalar})\s*$',  # line-leading unquoted pair
        ):
            hits = re.findall(pattern, raw)
            if hits:
                try:
                    out[name] = json.loads(hits[-1])
                    break
                except ValueError:
                    continue

    # A required free-text field the model wrote as prose instead of a quoted value: the prose is it.
    for name in required:
        if name in out:
            continue
        if props.get(name, {}).get("type") == "string" and raw.strip():
            out[name] = raw.strip()

    # A NON-SCALAR field the model clearly did write, which the scalar scan above cannot represent:
    # refuse to salvage rather than return an object with it missing. Omitting it would validate
    # whenever the field is optional, so the model's action would vanish with no error and no log --
    # the one outcome worse than a parse failure, because a lost customer action is indistinguishable
    # from one never attempted. Narrow on purpose: it fires only when the key is present in `raw`, was
    # not captured, and is declared as something other than a scalar, so the prose-and-trailing-score
    # rescue this function exists for is untouched.
    for name, prop in props.items():
        if name in out or not _is_nonscalar_field(prop):
            continue
        if re.search(rf'"{re.escape(name)}"\s*:', raw):
            return None

    if any(name not in out for name in required):
        return None
    return json.dumps(out)


def _is_nonscalar_field(prop: dict) -> bool:
    """True if `prop` declares an object/array (directly, via `$ref`, or inside a nullable `anyOf`).

    Used only to decide whether a key the model wrote but the salvage could not read is a data loss
    worth refusing over. A `$ref` counts without being resolved: a reference in this codebase's
    schemas always points at a nested model, never a scalar alias.
    """
    branches = [prop] + list(prop.get("anyOf") or [])
    return any(
        "$ref" in b or b.get("type") in ("object", "array") for b in branches
    )


def _first_json_object(text: str) -> str | None:

    """Return the first balanced {...} substring of text, or None. Brace-matching (string-aware)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None

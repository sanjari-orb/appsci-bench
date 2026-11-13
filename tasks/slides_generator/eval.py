"""Slides-generator eval functions: per-data-point, keyword-only eval functions implementing the
objectives declared in spec.yaml.

Six content judges (`prompt_satisfaction`, `structure_clarity`, `content_narrative`,
`expectations_satisfaction`, `content_coverage`, `groundedness`) score a JSON EXTRACTED FROM THE
PRODUCED `.pptx` (via `pptx_json.extract_presentation`), not the artifact's self-reported JSON --
see `_deck_conversation`/`_gated` below. This closes the saturation gap where a broken/unrenderable
deck still scored well because these judges only ever read the agent's claim. A deck whose `.pptx`
cannot be opened at all -- there is no JSON to hand a judge -- scores **0.0** on all six, never
`None` (which would renormalize the objective's weight away and raise the score -- exactly the bug
being fixed). A deck that DOES open but is degenerate (zero slides, or every slide's elements empty)
still produces a real extracted JSON and is passed through to the judge like any other deck: the
judge sees exactly what a viewer opening the deck would see -- an empty presentation -- and scores it
on its own reasoning rather than being pre-empted by a hard-coded floor. Speaker notes are stripped
before judging (not shown to any of the six).

`functional_success`, `output_format_compliance`, and `constraint_satisfaction` keep reading the
self-reported JSON: `output_format_compliance` in particular is the contract check ON THE CLAIM
(every slide declares slideNumber/layout/a non-empty element), and switching it to the extracted
JSON would make it unfalsifiable -- the extractor synthesizes slideNumber and layout, so those
checks could never fail.

`template_adherence` and `layout_quality` are vision judges: they render the relevant deck(s) to
PDF via LibreOffice (`helpers/render.py`) and return `None` per-sample (not an unconditional
placeholder) when rendering is unavailable, the template/output file is missing, or the configured
model lacks native PDF support. Already file-based; unaffected by the above.

`expectations_satisfaction` is the only objective that reads the per-sample `eval_rubric` channel --
see its docstring for why that is deliberate and must stay that way.
"""
from __future__ import annotations

import io
import json
from dataclasses import replace
from typing import Any, Callable

from pptx import Presentation

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn, File, _expected
from appsci_bench.eval.helpers import (
    make_source_coverage_judge, make_source_grounding_judge, make_transcript_judge,
    make_visual_comparison_judge, make_visual_quality_judge,
)
from appsci_bench.eval.helpers.render import render_pptx_to_pdf
from tasks.slides_generator.pptx_json import (
    _element_has_content, extract_presentation, has_duplicate_zip_parts,
)


# --- shared helpers ------------------------------------------------------------------------------

def _presentation_dict(conversation: list[ConversationTurn]) -> dict | None:
    """Return the inner presentation dict ({'slides': [...]}) from the latest response.json, in
    either of the two shapes the artifact may use ({'presentation': {...}} or top-level)."""
    if not conversation:
        return None
    raw = conversation[-1].response.json
    if not raw:
        return None
    try:
        output = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(output, dict):
        return None
    if "presentation" in output:
        p = output["presentation"]
        return p if isinstance(p, dict) else None
    if "slides" in output:
        return output
    return None


# The styling template's filename, matched as a case-insensitive SUBSTRING everywhere it is used:
# `_template_file` below (which attachment IS the template) and the source judges' exclusions further
# down (which attachments are NOT sources). Shared so those two can never disagree. A substring also
# catches the curation pipeline's "<stem>_template.pptx" naming; the full "template.pptx" rather than
# the bare word "template" keeps a genuine source file merely named e.g. "Q1_template_review.pptx"
# from being mistaken for it.
_TEMPLATE_NAME = "template.pptx"


def _pptx_file(conversation: list[ConversationTurn]):
    """Return the produced .pptx File from the latest response, or None."""
    if not conversation:
        return None
    for f in conversation[-1].response.files:
        if f.file_type == "pptx":
            return f
    return None


def _template_file(conversation: list[ConversationTurn]) -> File | None:
    """Return the query's attached styling template File from the latest turn, or None.

    Matched by the SAME rule the source judges exclude it by (`_TEMPLATE_NAME` as a case-insensitive
    substring -- see the note above the judge constructions below), so the two cannot disagree about
    which attachment is the template. They previously did: this lookup required the exact name while
    the exclusion was a substring match, so under the curation pipeline's `<stem>_template.pptx`
    naming the source judges correctly skipped the file and this lookup missed it, leaving
    `template_adherence` to return None with no error.
    """
    if not conversation:
        return None
    for f in conversation[-1].query.files:
        if _TEMPLATE_NAME in f.file_name.lower():
            return f
    return None


def _dedupe_zip(data: bytes) -> bytes:
    """Rewrite a zip archive so every member name appears exactly once, keeping the LAST entry
    written for any name that repeated (matching what `zipfile`'s own read-side resolution already
    does silently) and preserving the archive's original member order otherwise.

    Exists because `python-pptx`'s `Presentation.save()` can itself write a duplicate part name for
    certain decks (a known package quirk involving an orphaned notes-slide relationship) -- so a
    trim-and-resave in `_first_n_slides` can turn a clean deck into a corrupt one. `python-pptx` and
    stdlib `zipfile` both read straight past the duplicate (silently keeping the last entry), so the
    corruption is invisible to this codebase's own tooling; only a stricter OOXML parser like real
    PowerPoint's rejects the file and prompts to repair it. Deduping here removes the corruption at
    its source rather than relying on every reader downstream to tolerate it.
    """
    import zipfile

    src = zipfile.ZipFile(io.BytesIO(data))
    last_index: dict[str, int] = {}
    for i, info in enumerate(src.infolist()):
        last_index[info.filename] = i

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        seen: set[str] = set()
        for i, info in enumerate(src.infolist()):
            if info.filename in seen or last_index[info.filename] != i:
                continue
            seen.add(info.filename)
            dst.writestr(info, src.read(info.filename))
    return out.getvalue()


def _first_n_slides(pptx_bytes: bytes, n: int = 4) -> tuple[bytes | None, str | None, str | None]:
    """Return `(trimmed pptx bytes, None, degraded reason or None)` with only the first n slides
    kept, or `(None, failure reason, None)` if the deck can't be parsed or trimmed. A no-op (returns
    the input unchanged, with no degrade reason) when the deck already has <= n slides.

    Used before every render in this module: a rendered PDF sends as one whole document (no per-page
    selection), so capping page count means trimming the source deck first. Trim failure is reported
    rather than falling back to the untrimmed bytes -- rendering an untrimmed deck would send every
    page of an arbitrarily long deck to the judge and score it over slides these objectives declare
    out of scope, so the honest outcome is `None` (not applicable) for that sample.

    The third element is the `metadata["degraded"]` reason required by `helpers/files.py`'s
    "truncate, record it, never silent" rule: a trimmed deck yields a whole-deck score computed from
    a fraction of the deck, so every caller must surface that alongside the score. It is built here
    because this is the only place that knows the pre-trim slide count without re-parsing the deck.
    Note that no report renders this key today (only the stored per-data-point records carry it) --
    see KNOWN_ISSUES.md.

    Reaches into python-pptx's private `slides._sldIdLst` deliberately: the library exposes no public
    API for REMOVING a slide (only `add_slide`), so this is the only way to trim a deck in memory.
    """
    try:
        prs = Presentation(io.BytesIO(pptx_bytes))
        slide_ids = list(prs.slides._sldIdLst)
        total = len(slide_ids)
        if total <= n:
            return pptx_bytes, None, None
        for slide_id in slide_ids[n:]:
            prs.slides._sldIdLst.remove(slide_id)
        buf = io.BytesIO()
        prs.save(buf)
        return _dedupe_zip(buf.getvalue()), None, f"judged the first {n} of {total} slides"
    except Exception as exc:      # noqa: BLE001 -- corrupt deck or a python-pptx internals change
        return None, f"could not trim deck to first {n} slides: {exc}", None


def _open_pptx(file) -> tuple[bool, str]:
    """Try to open a File's bytes as a .pptx. Returns (ok, reason)."""
    if file is None:
        return False, "no .pptx file in the response"
    if has_duplicate_zip_parts(file.file_data):
        return False, "pptx archive has a duplicate part name -- corrupt, needs repair in PowerPoint"
    try:
        prs = Presentation(io.BytesIO(file.file_data))
    except Exception as exc:                        # noqa: BLE001 -- any open failure is a fail
        return False, f"python-pptx could not open file: {exc}"
    if len(prs.slides) == 0:
        return False, "pptx has zero slides"
    return True, ""


def _deck_conversation(
    conversation: list[ConversationTurn],
) -> tuple[list[ConversationTurn] | None, str]:
    """A COPY of `conversation` whose last turn's response carries the JSON extracted from the
    produced .pptx (speaker notes dropped), or (None, reason) when there is no JSON to extract at
    all -- no .pptx file in the response, the archive has a duplicate part name (see
    `pptx_json.has_duplicate_zip_parts`: `python-pptx` resolves that silently to the last entry
    written, so it opens and extracts clean even though real PowerPoint rejects the file and prompts
    to repair it), or python-pptx cannot open the file's bytes (see `pptx_json.extract_presentation`).
    A deck that opens but has zero slides, or slides with no text anywhere, still produces a real (if
    degenerate) JSON and is passed through to the judge rather than gated here -- the judge sees
    exactly what a human opening the deck would see, and decides for itself how to score that content.

    Three field decisions on the rewritten response, each load-bearing:
      - `text=None` is mandatory, not optional. Over the completions transport, `artifact_client.py`
        fills `text` with the raw reply and `json` from that SAME reply, so the agent's
        self-reported JSON usually sits in `text` verbatim. `_render_io`'s dedupe (drop `json` when
        it's already a substring of `text`) would then fail to fire and a transcript judge would be
        shown both the claimed JSON and the extracted one. Clearing `text` guarantees only the
        extracted `json` line renders.
      - `files` is left untouched: transcript judges render a file-existence line from it, and the
        source judges read `query.files`, not `response.files` -- nothing here needs to change it.
      - `trace=None`: the agent's self-report, not rendered by either judge family; clearing it costs
        nothing and avoids carrying it into a rewritten record that claims to be deck-derived.
    The copy is shallow -- `File` objects and their bytes are shared with the original -- since
    nothing downstream mutates them and deep-copying a multi-MB deck per objective would be wasted
    work.
    """
    file = _pptx_file(conversation)
    if file is None:
        return None, "no .pptx file in the response"
    if has_duplicate_zip_parts(file.file_data):
        return None, "pptx archive has a duplicate part name -- corrupt, needs repair in PowerPoint"
    extracted = extract_presentation(file.file_data, include_notes=False)
    if extracted is None:
        return None, "python-pptx could not open the file -- no JSON to extract"
    last = conversation[-1]
    new_response = replace(
        last.response, json=json.dumps(extracted, ensure_ascii=False), text=None, trace=None)
    return [*conversation[:-1], replace(last, response=new_response)], ""


def _gated(
    judge: Callable[..., tuple[float | None, dict]], *, data_point_id: str,
    conversation: list[ConversationTurn], expected_output: ArtifactAgentOutput | None,
    eval_rubric: str | None, **kwargs: Any,
) -> tuple[float, dict]:
    """Run `judge` over the deck-derived conversation (see `_deck_conversation`); 0.0 -- never
    `None` -- when there is no JSON to hand the judge at all (no .pptx file in the response, or
    python-pptx cannot open its bytes). `None` would renormalize this objective's weight away and
    RAISE the score, which is the saturation this gate exists to prevent.

    A deck that opens but is otherwise degenerate -- zero slides, or every slide's elements empty --
    is NOT gated here: `_deck_conversation` still produces a real JSON for it, and it is handed to
    the judge like any other deck. The judge's own reasoning decides how that scores; this gate only
    covers the one case where there is no JSON to even attempt scoring.

    A legitimate `None` the judge itself returns (token overflow, no source files, source-survival
    floor) passes through untouched: the gate only intercepts BEFORE the judge is called, for the
    unopenable-file case specifically, so those measurement-was-not-possible outcomes are
    unaffected."""
    deck_conversation, reason = _deck_conversation(conversation)
    if deck_conversation is None:
        return 0.0, {"reason": f"no JSON could be extracted from the produced .pptx: {reason}",
                     "scored_from": "produced_pptx"}
    score, metadata = judge(
        data_point_id=data_point_id, conversation=deck_conversation,
        expected_output=expected_output, eval_rubric=eval_rubric, **kwargs)
    return score, {**metadata, "scored_from": "produced_pptx"}



# --- deterministic (3) ---------------------------------------------------------------------------

def functional_success(*, data_point_id: str, conversation: list[ConversationTurn],
                       expected_output: ArtifactAgentOutput | None = None,
                       eval_rubric: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """A run passes when BOTH hold: output carries a JSON presentation object with a non-empty
    slides array, AND a .pptx file that opens as a non-duplicate-part archive with python-pptx and
    has at least one slide (see `_open_pptx`). Content quality is scored separately by the rubric
    objectives."""
    del data_point_id, expected_output, eval_rubric, kwargs
    presentation = _presentation_dict(conversation)
    if presentation is None:
        return 0.0, {"reason": "output not a presentation object"}
    slides = presentation.get("slides")
    if not isinstance(slides, list) or len(slides) == 0:
        return 0.0, {"reason": "JSON slides array absent or empty"}
    ok, reason = _open_pptx(_pptx_file(conversation))
    if not ok:
        return 0.0, {"reason": f"pptx check failed: {reason}"}
    return 1.0, {"json_slide_count": len(slides)}


def _check_slide(slide: Any, index: int) -> tuple[bool, str]:
    if not isinstance(slide, dict):
        return False, f"slide[{index}] is not a dict"
    slide_num = slide.get("slideNumber")
    if slide_num is None or str(slide_num).strip() == "":
        return False, f"slide[{index}] missing slideNumber"
    layout = slide.get("layout")
    if not layout or not str(layout).strip():
        return False, f"slide[{index}] missing layout"
    elements = slide.get("elements")
    if not isinstance(elements, list) or len(elements) == 0:
        return False, f"slide[{index}] has no elements"
    if not any(_element_has_content(e) for e in elements if isinstance(e, dict)):
        return False, f"slide[{index}] has no element with non-empty content"
    return True, ""


def output_format_compliance(*, data_point_id: str, conversation: list[ConversationTurn],
                             expected_output: ArtifactAgentOutput | None = None,
                             eval_rubric: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """Every slide must have a non-empty slideNumber, layout, and at least one element with
    non-empty content. Scored as the PROPORTION of compliant slides, not all-or-nothing: one
    malformed slide in eleven is a very different outcome from a wholly malformed deck, and
    collapsing both to 0.0 threw away that signal."""
    del data_point_id, expected_output, eval_rubric, kwargs
    presentation = _presentation_dict(conversation)
    if presentation is None:
        return 0.0, {"reason": "output is not a presentation object"}
    slides = presentation.get("slides", [])
    if not isinstance(slides, list) or len(slides) == 0:
        return 0.0, {"reason": "slides array absent or empty"}
    violations = [reason for i, slide in enumerate(slides)
                  for ok, reason in [_check_slide(slide, i)] if not ok]
    score = (len(slides) - len(violations)) / len(slides)
    return score, {"violations": violations, "slide_count": len(slides),
                   "compliant_slide_count": len(slides) - len(violations)}


def constraint_satisfaction(*, data_point_id: str, conversation: list[ConversationTurn],
                            expected_output: ArtifactAgentOutput | None = None,
                            eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """A run passes when the produced slide count falls within
    `expected_output.json`'s `user_constraints.expected_slide_count_range` [min, max] (inclusive).
    `None` if that range isn't present on this sample (nothing to check against)."""
    del data_point_id, eval_rubric, kwargs
    expected = (_expected(expected_output) or {})
    user_constraints = expected.get("user_constraints")
    slide_range = user_constraints.get("expected_slide_count_range") if isinstance(user_constraints, dict) else None
    if not isinstance(slide_range, list) or len(slide_range) != 2:
        return None, {"applicable": False}
    presentation = _presentation_dict(conversation)
    slides = presentation.get("slides") if presentation else None
    count = len(slides) if isinstance(slides, list) else 0
    lo, hi = slide_range
    ok = lo <= count <= hi
    return (1.0 if ok else 0.0), {"applicable": True, "slide_count": count, "expected_range": slide_range}


# --- rubric via make_transcript_judge (4) --------------------------------------------------------
# prompt_satisfaction, structure_clarity, content_narrative and expectations_satisfaction judge the
# CONVERSATION (brief + generated slides text/json) only, so they use make_transcript_judge.
# content_coverage moved to a source-reading judge below (it must see attached-file CONTENT, which
# the transcript judge is blind to). Each prompt below uses make_transcript_judge's exposed
# variables (transcript/eval_rubric/expected_text/expected_json) rather than task-specific ones like
# {{ brief }}/{{ generated_slides }} -- the rendered transcript already carries both the query (the
# brief) and the response (the generated slides text/json).
#
# Only expectations_satisfaction references {{ eval_rubric }}; see its docstring for the rule.

_FORMAT_COMPLIANCE_TEMPLATE = """\
    Respond with a single JSON object with exactly these two keys, no others: `reasoning` (string,
your step-by-step evaluation) and `score` (number, 0-1).
"""

_PROMPT_SATISFACTION_TEMPLATE = """\
You are an expert evaluator assessing whether a generated slide deck did what the user actually
ASKED FOR. This is instruction-following, not fact coverage: judge whether the brief's explicit
requests were honoured, NOT whether source documents were faithfully mined.

Judge these, to the extent the brief states them:
- Every explicit instruction in the brief is honoured (what to include, what to leave out, what to
  emphasise, required sections, requested ordering).
- The requested audience and register are hit (exec vs technical, persuasive vs informative) --
  getting the content right but pitching it at the wrong level of formality or seniority is a real
  miss, not a stylistic nuance.
- Stated exclusions and formatting requests are respected.
- Requested emphasis is reflected in how much of the deck is spent on what.

## Scoring Criteria (0-1 Scale)

**0.8-1.0 (Excellent)**: Every explicit instruction is honoured. Audience and register match what
was asked -- the deck's tone and level of formality fit who it says it is for. Nothing the brief
excluded appears anywhere in the deck; nothing it required is missing.

**0.6-0.79 (Good)**: Every stated exclusion is respected -- a topic the brief said to leave out
reappearing anywhere in the deck disqualifies this band regardless of how well everything else is
followed, since violating an exclusion is a stronger failure than underserving an inclusion. The
audience/register match is also disqualifying at this band: a deck pitched at a visibly wrong level
of formality or seniority for its stated audience does not belong here even if every individual
instruction was otherwise followed; it belongs in Fair. Within that constraint: most
instructions honoured, a minor request is underserved or an emphasis is slightly off, but nothing the
brief asked for is ignored outright.

**0.4-0.59 (Fair)**: About half the instructions are followed, or a stated exclusion is violated, or
the register/audience is wrong for what was asked (e.g. a board-level ask answered with a routine
status-update tone, or the reverse) -- any one of these caps the score here regardless of how well
other instructions were followed.

**0.2-0.39 (Poor)**: Most instructions ignored. The deck is on-topic but is not the deck that was
requested.

**0.0-0.19 (Critical)**: The brief's instructions are disregarded, contradicted, or the deck answers
a different request entirely.

## Requested output shape (for reference, if useful)
{{ expected_json }}

## Conversation to judge (the brief given as the query; the generated slides as the response)
{{ transcript }}

Evaluate step by step: (1) enumerate the explicit instructions and requests in the brief, including
exclusions; (2) check each against the deck, checking exclusions FIRST since a violated exclusion caps
the score regardless of other findings; (3) judge audience/register fit SECOND, since a wrong register
also caps the score regardless of other findings -- name what register was asked for and what the
deck actually reads as; (4) give a final score and explanation naming any instruction that was not
followed.
""" + _FORMAT_COMPLIANCE_TEMPLATE

_STRUCTURE_CLARITY_TEMPLATE = """\
You are an expert presentation architect evaluating the logical flow and structural organization
of a slide deck: progression and flow between slides, a clear narrative arc (introduction -> main
content -> conclusion), appropriate transitions, alignment with the brief's audience and purpose,
and section/hierarchy clarity.

Being correctly ORDERED is not the same as HAVING STRUCTURE. A deck can sequence its slides in a
sensible order (background, then detail, then a close) and still be a list, not an argument, if
nothing on it establishes why the topic matters before diving in, or lands anywhere at the end. Judge
whether the deck does the latter, not just the former.

## Scoring Criteria (0-1 Scale)

**0.8-1.0 (Excellent)**: A real beginning (states why this matters / sets up the stakes), a middle
that builds a case section by section, and an end that lands a point or a decision -- not merely the
last slide in the sequence. Transitions are smooth and purposeful throughout.

**0.6-0.79 (Good)**: The beginning/middle/end test above is met, but with a rough edge: one transition
is less smooth than the rest, or the close is present but slightly undersold. A deck that is well
SEQUENCED but does not clear the beginning/middle/end bar -- ordered slides with no stated stakes and
no landed conclusion -- belongs in Fair, not here, however clean the ordering looks.

**0.4-0.59 (Fair)**: Some organizational structure but unclear in places; transitions sometimes
abrupt; narrative arc partially visible but incomplete -- including a deck that is correctly ordered
throughout but never establishes stakes at the start or a conclusion at the end.

**0.2-0.39 (Poor)**: Lacks clear structure; slides seem randomly ordered or loosely connected;
transitions unclear.

**0.0-0.19 (Critical)**: No discernible structure; slides disorganized; presentation incoherent.

## Conversation to judge (the brief as the query; the generated slides as the response)
{{ transcript }}

Evaluate step by step: (1) identify the intended structure and narrative arc from the brief;
(2) name the deck's beginning (stakes), middle (case), and end (landed point) explicitly, or state
which is missing -- a deck that is merely well-ordered without one of these three does not clear
Good; (3) trace the logical flow through each section; (4) assess clarity of transitions;
(5) judge overall coherence relative to the brief; (6) give a score and detailed explanation.
""" + _FORMAT_COMPLIANCE_TEMPLATE


# content_narrative scores the deck's STORY ARC, inspired by EvoPresent's "Narrative" dimension.
# It deliberately overlaps `structure_clarity` above, which scores logical flow and transitions --
# kept as two separate objectives, with the correlation handled by weighting in spec.yaml (0.75
# each) rather than by merging them.
_CONTENT_NARRATIVE_TEMPLATE = """\
You are an expert presentation evaluator judging ONLY the deck's NARRATIVE -- whether it tells a
coherent, compelling story. Do not judge visual design, factual accuracy, or source coverage; those
are scored by separate objectives.

Assess:
- **Arc**: a clear beginning (motivation/problem), middle (approach/substance), and end (results,
  takeaway, or ask).
- **Cohesion**: transitions and signposting between sections; the audience can follow why one slide
  follows another.
- **Progression**: each slide advances the story; no tangents, no stalling, no redundant slides.
- **Framing**: the stakes, context, and relevance to the stated audience are established and then
  resolved by the end.

## Scoring Criteria (0-1 Scale)

**0.8-1.0 (Excellent)**: Strong arc with seamless transitions and explicit takeaways; every slide
earns its place; the deck makes a case rather than just presenting slides in a sensible order.

**0.6-0.79 (Good)**: A real storyline that resolves -- stakes are established and the end lands a
takeaway or ask -- but with a minor weak transition or a missing signpost. A deck whose slides are in
a defensible order but that never establishes stakes, or never resolves them, is NOT a "clear
storyline" and belongs in Fair even if every individual slide reads cleanly.

**0.4-0.59 (Fair)**: Sections exist but connections are loose; some slides feel isolated from the
story -- including a deck that is merely correctly sequenced without a stated beginning or a landed
end.

**0.2-0.39 (Poor)**: Fragmented; jumps in logic; important steps of the story are missing.

**0.0-0.19 (Critical)**: Disjoint; no discernible arc and no resolution.

## Conversation to judge (the brief as the query; the generated slides as the response)
{{ transcript }}

Evaluate step by step: (1) identify the arc's beginning, middle, and end, or note what is missing --
being in the right order is not the same as having an arc, so name the stakes and the resolution
explicitly rather than inferring them from slide order; (2) trace cohesion and signposting slide to
slide; (3) flag any slide that does not advance the story; (4) judge whether the framing is resolved;
(5) give a score and detailed explanation.
""" + _FORMAT_COMPLIANCE_TEMPLATE


# expectations_satisfaction is the ONLY objective that consumes the per-sample `eval_rubric`
# channel -- see the wrapper function below for why, and for the rule that no other prompt in this
# module may reference `{{ eval_rubric }}`.
#
# Scored by PROPORTION of expectations met rather than by impression, following DeepPresenter's
# rule-based Constraint Score idea (fraction of user-specified constraints satisfied). The rubric
# text is a checklist, so a fraction is the honest summary of it, and the per-expectation verdicts
# make a low score diagnosable.
#
# NOT VISUAL: this is a transcript judge, so it sees the JSON presentation object and text and
# NEVER a render. The per-sample rubric notes are therefore scoped to content and structure only
# (see eval_data/README.md). Visual expectations belong to `template_adherence`.
_EXPECTATIONS_SATISFACTION_TEMPLATE = """\
You are an expert evaluator checking a generated slide deck against a specific, hand-written list of
expectations for THIS request. The expectations below are authoritative: they encode what this
particular deck was required to get right, including named failure modes to avoid.

Some expectations are marked "THE CRUX" -- this means the sample was specifically built to test that
one behavior, and getting it wrong is the single biggest failure mode for this request. A deck that
misses a CRUX expectation has failed at what this request exists to test, regardless of how well it
did on everything else, so a `missed` verdict on any CRUX item caps the WHOLE score at 0.3 -- do not
average it away against unrelated items that passed.

Some expectations bundle several distinct sub-claims into one numbered item (e.g. four separate
figures or four separate placements in a single sentence). Do not give `met` to a bundled item on a
holistic impression -- check each sub-claim individually and name any that failed, and downgrade to
`partially met` or `missed` per the rule in step 2 below if any of them did not hold.

Score by PROPORTION SATISFIED, not by overall impression.

You see the deck as structured data and text -- NOT as a rendered image. Judge only what the text and
structure show. If an expectation is about visual appearance (colors, fonts, styling, alignment,
"looks like"), mark it `not assessable` and EXCLUDE it from the proportion rather than guessing.

## Expectations for this request
{{ eval_rubric }}

## Conversation to judge (the brief as the query; the generated slides as the response)
{{ transcript }}

Evaluate step by step:
1. Enumerate each expectation above as a separate numbered item, flagging any marked "THE CRUX".
   If an item bundles multiple sub-claims, list each sub-claim separately in your reasoning before
   giving the item's single verdict.
2. For each, give a verdict -- `met`, `partially met`, `missed`, or `not assessable` -- with the
   slide number and a short quote or description as evidence. A "do not do X" expectation is `met`
   when the deck does not do X. Reserve `partially met` for an expectation that is SUBSTANTIVELY
   addressed but incomplete -- e.g. three of four required sub-claims correct, or the right idea
   stated without every required detail. A token gesture toward an expectation -- it is mentioned in
   passing, or addressed in a way that misses the actual point of the expectation -- is `missed`, not
   `partially met`; do not use `partially met` to soften what is really a miss. For a bundled item,
   `met` requires EVERY sub-claim to hold -- one wrong figure or one wrong placement among several
   makes the item at most `partially met`, never `met`.
3. Check whether any CRUX-marked expectation is `missed`. If so, the final score is capped at 0.3
   regardless of the arithmetic in step 4 -- state this explicitly in your reasoning and report the
   capped score, not the uncapped fraction.
4. Otherwise, count: score = (met + 0.5 * partially met) / (total expectations MINUS not-assessable
   ones). If that denominator is zero (every expectation was not assessable), score 0.
5. Report the per-expectation verdicts and that arithmetic in your reasoning, then give the score.
""" + _FORMAT_COMPLIANCE_TEMPLATE


_GROUNDEDNESS_RUBRIC = """\
## Scoring Criteria (0-1 Scale)

When the input is sparse or a real gap exists in the source material, the correct response is to
flag the gap or leave it visibly open -- not to invent a plausible-sounding specific to fill it.

**0.8-1.0 (Excellent)**:
EVERY specific figure, name, date, and claim in the deck is individually traceable to the source
documents -- not merely "no fabrication detected" on a casual read, but verified claim by claim.
Nothing beyond what the source provided is presented as fact or figure. Caveats retained. Deck
neither overstates nor understates what the sources say. A genuine gap in the source is left visible
(flagged, or simply absent) rather than filled with an invented specific.

**0.6-0.79 (Good)**:
Every specific number and named entity is grounded. One or two minor additions that are not in the
source but are plausible inferences or standard business phrasing — never a fabricated number, date,
or named entity, which disqualifies this band regardless of how minor it seems elsewhere.

**0.4-0.59 (Fair)**:
At least one specific figure, date, or named entity cannot be traced to any source document. Deck may
have filled a gap with a plausible-sounding approximation or a softened caveat.

**0.2-0.39 (Poor)**:
Multiple invented figures, names, or claims. Deck presents information as if sourced when it is not.

**0.0-0.19 (Critical)**:
Substantial fabrication. Key figures, names, or conclusions are made up. Deck cannot be trusted against the source material.


Please evaluate step by step:
1. Extract all specific claims (numbers, names, dates, percentages) from the generated slides
2. For each claim, check whether it appears in the source documents -- do not credit "Excellent"
   without having actually checked each one individually
3. Flag any claim that cannot be traced to a source
4. Check whether caveats or limitations from sources survive into the deck
5. Provide score and detailed explanation
""" + _FORMAT_COMPLIANCE_TEMPLATE



_CONTENT_COVERAGE_RUBRIC = """\
## Scoring Criteria (0-1 Scale)

First use the brief (the REQUEST section, if shown) to decide what is even in scope: a source fact
the brief explicitly excluded, or that falls outside a stated focus ("focus on X", "just cover the
Q3 numbers", "skip the appendix"), is OUT OF SCOPE and must not be counted as missing coverage no
matter how prominent it is in the source -- the deck was asked to cover the brief's scope, not the
whole source. When the brief states no such scoping, treat the full source as in scope.

Within scope, separate source facts into two tiers: MATERIAL (a number, decision, name, or caveat
that changes what the reader understands or does -- the kind of fact a requester would flag as
important if asked) and INCIDENTAL (color/detail that would not change the takeaway if dropped). The
bands below turn on the MATERIAL tier; incidental omissions never cap a score below Good on their own.

**0.8-1.0 (Excellent)**:
Every MATERIAL in-scope fact, figure, metric, and data point from the source materials appears in the
deck, faithfully represented -- nothing a requester would consider important is missing. At most
incidental detail is left out.

**0.6-0.79 (Good)**:
Every MATERIAL in-scope fact is covered. One or two INCIDENTAL, non-critical details are omitted --
but no fact a reasonable reader would call load-bearing is missing; a single missing material
in-scope fact disqualifies this band regardless of how well everything else is covered.

**0.4-0.59 (Fair)**:
At least one MATERIAL in-scope fact is missing, or roughly half the in-scope source content overall
is present. The audience would notice a meaningful gap where a fact that mattered is missing.

**0.2-0.39 (Poor)**:
Multiple material in-scope facts are missing. In-scope source material is substantially
underrepresented.

**0.0-0.19 (Critical)**:
Most in-scope source content, material or otherwise, is absent. The deck fails to capture the
source material it was asked to cover.


Please evaluate step by step:
1. Read the REQUEST (if shown) and note any stated focus or exclusion that narrows what counts as
   in scope
2. Enumerate the material facts and key figures (numbers, names, dates, percentages, metrics)
   present in the SOURCE materials that fall IN SCOPE per step 1, marking each MATERIAL or
   INCIDENTAL per the definition above -- an out-of-scope fact is not enumerated at all
3. For each in-scope source fact, check whether it is captured in the generated slides
4. Note any MATERIAL in-scope source fact that was silently dropped -- this is what separates Good
   from Fair
5. Do NOT penalize the deck for adding extra content, and do NOT reward it merely for avoiding
   fabrication (that is groundedness, a separate objective) -- judge omission, not invention
6. Provide score and detailed explanation
""" + _FORMAT_COMPLIANCE_TEMPLATE



_prompt_satisfaction_judge = make_transcript_judge(_PROMPT_SATISFACTION_TEMPLATE)
_structure_clarity_judge = make_transcript_judge(_STRUCTURE_CLARITY_TEMPLATE)
_content_narrative_judge = make_transcript_judge(_CONTENT_NARRATIVE_TEMPLATE)

_expectations_judge = make_transcript_judge(_EXPECTATIONS_SATISFACTION_TEMPLATE)


def prompt_satisfaction(*, data_point_id: str, conversation: list[ConversationTurn],
                        expected_output: ArtifactAgentOutput | None = None,
                        eval_rubric: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """`_prompt_satisfaction_judge` scored against the JSON extracted from the produced .pptx
    (via `_gated`/`_deck_conversation`), not the artifact's self-reported JSON -- see module
    docstring. 0.0, never `None`, when the .pptx cannot be opened at all."""
    return _gated(_prompt_satisfaction_judge, data_point_id=data_point_id, conversation=conversation,
                 expected_output=expected_output, eval_rubric=eval_rubric, **kwargs)


def structure_clarity(*, data_point_id: str, conversation: list[ConversationTurn],
                      expected_output: ArtifactAgentOutput | None = None,
                      eval_rubric: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """`_structure_clarity_judge` scored against the JSON extracted from the produced .pptx -- see
    module docstring. 0.0, never `None`, when the .pptx cannot be opened at all."""
    return _gated(_structure_clarity_judge, data_point_id=data_point_id, conversation=conversation,
                 expected_output=expected_output, eval_rubric=eval_rubric, **kwargs)


def content_narrative(*, data_point_id: str, conversation: list[ConversationTurn],
                      expected_output: ArtifactAgentOutput | None = None,
                      eval_rubric: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """`_content_narrative_judge` scored against the JSON extracted from the produced .pptx -- see
    module docstring. 0.0, never `None`, when the .pptx cannot be opened at all."""
    return _gated(_content_narrative_judge, data_point_id=data_point_id, conversation=conversation,
                 expected_output=expected_output, eval_rubric=eval_rubric, **kwargs)


def expectations_satisfaction(*, data_point_id: str, conversation: list[ConversationTurn],
                              expected_output: ArtifactAgentOutput | None = None,
                              eval_rubric: str | None = None,
                              **kwargs: Any) -> tuple[float | None, dict]:
    """Scores the deck against this sample's hand-written expectations, passed in through the
    per-sample `eval_rubric` channel (distilled from the sample's EXPECTATIONS.md), against the
    JSON extracted from the produced .pptx (see module docstring).

    This is the ONLY objective in this module that reads `eval_rubric`, and that is deliberate:
    `eval_rubric` is the one input that VARIES per data point, so routing it into a single dedicated
    objective keeps every other metric's meaning identical across samples. Do not add
    `{{ eval_rubric }}` to another prompt here -- a metric whose definition shifts sample to sample
    is not comparable across samples, and `tests/tasks/slides_generator/test_eval.py` asserts this.

    `None` (not applicable) when the sample declares no expectations, matching
    `constraint_satisfaction`'s convention: nothing declared means nothing to check, and `None` is
    excluded from aggregation rather than charged to the agent as a 0.0. This check runs BEFORE the
    unopenable-file gate deliberately: "no rubric" means not-applicable-to-this-sample regardless of
    whether the .pptx can be opened, a different condition in kind from "the deck can't be read at
    all" -- scoring 0.0 here would charge the agent for a sample author not writing a rubric."""
    if eval_rubric is None or not eval_rubric.strip():
        return None, {"applicable": False, "reason": "sample declares no eval_rubric expectations"}
    return _gated(_expectations_judge, data_point_id=data_point_id, conversation=conversation,
                 expected_output=expected_output, eval_rubric=eval_rubric, **kwargs)


# --- coverage + groundedness (source-reading) --------------------------------------------------
# Both return `None` (not an error/raise) when an input they need is unavailable: an error is forced
# to the worst score (0.0) forever, wrongly charging a missing input to the agent. `None` excludes
# the sample from aggregation instead, and the aggregator's own "always None" warning surfaces a
# systematic gap loudly without permanently tanking the task's score.
# The unopenable-file case is different in kind from those input-unavailable cases -- see `_gated`
# -- so it is scored 0.0, never None, via the same gate the four transcript judges above go through.

# content_coverage and groundedness are the two Level-2 file-content judges: both read the query's
# INPUT source files and check the produced deck (as the JSON extracted from the produced .pptx --
# see module docstring), NOT make_transcript_judge -- the transcript judge is blind to attached-file
# CONTENT, so it could never see the source facts. They ask OPPOSITE questions of the same inputs:
#   * content_coverage (make_source_coverage_judge): what PROPORTION of the source facts the deck
#     covers -- it penalizes silently-dropped source content and degrades (truncates source text,
#     still scores) on overflow. This is distinct from prompt_satisfaction above, which is a
#     transcript judge measuring whether the deck followed the PROMPT's explicit instructions;
#     content_coverage also reads the brief (`include_query_text=True`, baked into
#     make_source_coverage_judge), but only to SCOPE what counts as material -- a fact the brief
#     excluded or placed outside a stated focus is not a coverage miss, which matters most on a
#     large source the brief has deliberately narrowed down.
#   * groundedness (make_source_grounding_judge): whether the deck INVENTS content the source does
#     not support -- it hard-bails to None on overflow (truncating a source figure would corrupt the
#     very check it performs). Does not read the brief: a fabricated figure is ungrounded regardless
#     of what was asked for.
# Both exclude the styling template by `_TEMPLATE_NAME` as a substring -- see that constant for why
# the full filename rather than the bare word "template", and note that `_template_file` uses the same
# rule so the "is the template" and "is not a source" decisions cannot diverge.
#
# Each metric's task-wide rubric (_CONTENT_COVERAGE_RUBRIC / _GROUNDEDNESS_RUBRIC) is that METRIC's own
# definition -- baked into the judge's system prompt at construction time via `metric_rubric=`,
# exactly like _PROMPT_SATISFACTION_TEMPLATE etc. are baked into their `make_transcript_judge` prompt
# templates. It is NOT the per-sample `EvalSample.eval_rubric` channel (an optional per-data-point NL
# note, unrelated to this metric's own definition). `scoring.py` passes `sample.eval_rubric` to every
# objective's function uniformly; `expectations_satisfaction` is this task's only intended reader of
# that channel (see the module docstring), so it is withheld here before delegating to the factory.
_content_coverage_judge = make_source_coverage_judge(
    exclude_name_substrings=(_TEMPLATE_NAME,), metric_rubric=_CONTENT_COVERAGE_RUBRIC)
_groundedness_judge = make_source_grounding_judge(
    exclude_name_substrings=(_TEMPLATE_NAME,), metric_rubric=_GROUNDEDNESS_RUBRIC)


def content_coverage(*, data_point_id: str, conversation: list[ConversationTurn],
                     expected_output: ArtifactAgentOutput | None = None,
                     eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """`make_source_coverage_judge` result, scored against the JSON extracted from the produced
    .pptx (see module docstring), with the per-sample `eval_rubric` withheld -- see the module
    docstring: `expectations_satisfaction` is this task's only reader of that channel. Also sends
    the query's brief text to the judge so a large source's coverage is scored against what the
    brief actually asked to be covered, not the whole source regardless of stated scope/exclusions
    (see `_CONTENT_COVERAGE_RUBRIC`). 0.0, never `None`, when the .pptx cannot be opened at all; a
    legitimate `None` (no source files, overflow) still passes through untouched -- see `_gated`."""
    del eval_rubric
    return _gated(_content_coverage_judge, data_point_id=data_point_id, conversation=conversation,
                 expected_output=expected_output, eval_rubric=None, **kwargs)


def groundedness(*, data_point_id: str, conversation: list[ConversationTurn],
                 expected_output: ArtifactAgentOutput | None = None,
                 eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """`make_source_grounding_judge` result, scored against the JSON extracted from the produced
    .pptx (see module docstring), with the per-sample `eval_rubric` withheld -- see the module
    docstring: `expectations_satisfaction` is this task's only reader of that channel. 0.0, never
    `None`, when the .pptx cannot be opened at all; a legitimate `None` (no source files, overflow)
    still passes through untouched -- see `_gated`."""
    del eval_rubric
    return _gated(_groundedness_judge, data_point_id=data_point_id, conversation=conversation,
                 expected_output=expected_output, eval_rubric=None, **kwargs)


# --- template_adherence + layout_quality (real, vision judges over rendered PDFs) ----------------
# Both render a deck's first 4 slides to PDF via helpers/render.py (LibreOffice headless) and judge
# the rendering natively. `None` is returned per-sample (not an unconditional placeholder) when the
# template/output pptx is missing, rendering fails (e.g. no LibreOffice on this host), or the
# configured model lacks native PDF support -- see make_visual_comparison_judge/
# make_visual_quality_judge in helpers/files.py for the overflow/capability-gating bail policy
# (native PDF blocks can't be truncated, so overflow bails to None rather than degrading).
#
# These two are the task's ONLY visual objectives. Known gaps are recorded in KNOWN_ISSUES.md --
# chiefly color CONTRAST as a standalone quality (template_adherence catches a *wrong* palette, not
# a consistently-applied low-contrast one) and the fact that both judges score a whole 4-slide PDF,
# so a defect past slide 4 is invisible no matter what. `template_adherence`'s top band now requires
# every shown slide to be clean (a single broken slide among the first 4 caps it below 0.9), which
# narrows -- but does not eliminate -- the older "one broken slide averages away" failure mode; it
# only reaches within the 4 slides actually judged. A deck trimmed to fit that cap is reported in
# `metadata["degraded"]` rather than scored silently (helpers/files.py's rule); no report renders
# that key yet, so it currently reaches only the stored per-data-point records.

_TEMPLATE_ADHERENCE_SYSTEM_PROMPT = """\
You are evaluating whether a generated slide deck (ACTUAL) visually adopted the styling of a
provided template deck (REFERENCE): color palette, font choices, and layout structure. Judge ONLY
visual/stylistic adherence -- NOT content, text, or substance similarity. You are shown up to the
first 4 slides of each deck; judge only what you are shown.

## Scoring Criteria (0-1 Scale)

**0.9-1.0 (Excellent)**: EVERY shown slide of ACTUAL clearly adopts the REFERENCE's color palette,
fonts, and layout conventions (title placement, spacing, decorative elements), with no exception. A
viewer would recognize both as belonging to the same visual family on every slide shown, not just
most of them.

**0.60-0.89 (Good)**: ACTUAL follows the REFERENCE's style overall -- palette, fonts, AND layout
conventions are all genuinely present, not just one of the three -- but at least one shown slide has a
visible deviation in color, font, or layout choice. A deck that only picked up the color while fonts
and layout stay generic does NOT belong here (adopting one dimension while ignoring the rest is not
broad stylistic adherence); it belongs in Fair.

**0.4-0.59 (Fair)**: ACTUAL partially reflects the REFERENCE's style -- some elements match (e.g.
color) but others clearly diverge (e.g. font family, layout structure). This includes a deck that
matches only one of palette/font/layout while the others read as generic and unstyled.

**0.2-0.39 (Poor)**: ACTUAL bears little visual resemblance to REFERENCE; most stylistic choices
diverge.

**0.0-0.19 (Critical)**: ACTUAL ignores the REFERENCE's styling entirely -- a generic or
default-looking deck with no visible relationship to the template.

Evaluate step by step: (1) identify the REFERENCE's color palette, fonts, and layout conventions;
(2) check EACH shown slide of ACTUAL individually against ALL THREE (palette, fonts, layout) --
matching only one dimension while the others are generic must not be scored as if it were broad
adherence; (3) give a final score and explanation naming any deviating slide and which of the three
(palette/font/layout) it deviated on.
""" + _FORMAT_COMPLIANCE_TEMPLATE

# layout_quality is inspired by AutoPresent's `layout` rubric and EvoPresent's Layout and Hierarchy
# dimensions. AutoPresent scores this with a VLM on a pixel render, so a VLM over the rendered PDF
# fits that approach -- note though that a GEOMETRIC check from python-pptx bounding boxes would be
# strictly more reliable for overlap/off-slide/margins (available to us and deliberately deferred --
# see KNOWN_ISSUES.md).
_LAYOUT_QUALITY_SYSTEM_PROMPT = """\
You are an expert presentation designer evaluating the LAYOUT of a rendered slide deck: the
placement, alignment, spacing, and visual hierarchy of elements. Judge layout only -- NOT content
correctness, narrative structure, or whether the deck matches a brand template, which are scored by
separate objectives.

Assess:
- **Bounds and overlap**: elements must not overlap each other, and nothing may exceed the slide
  page. No clipped or cut-off text.
- **Alignment and margins**: elements align to a consistent grid; consistent margins and gutters;
  sufficient spacing between elements.
- **Balance and whitespace**: visual weight is distributed sensibly; purposeful whitespace; slides
  are neither cramped nor conspicuously empty.
- **Reading order**: a natural top-to-bottom, left-to-right path through each slide.
- **Hierarchy**: a clear and CONSISTENT typographic order (title, then subtitle, then body, then
  annotation), so the most important element on each slide is immediately identifiable.

**Gating rule, checked before the bands below**: any element overlap, any element running off the
slide page, any clipped/cut-off text, or any slide where a viewer cannot immediately tell what to read
first -- on ANY slide shown, however small -- caps the score at Fair (0.4-0.59) or below. This is not
one factor among several: a deck that is otherwise clean but has one overlapping element, or one slide
where the most important thing to read is not obvious, is not "mostly strong," because a viewer hits
that defect immediately regardless of how well everything else is aligned.

## Scoring Criteria (0-1 Scale)

**0.8-1.0 (Excellent)**: Clean grid, consistent alignment and margins, ample purposeful whitespace,
clear reading path, unmistakable and consistent hierarchy -- on every shown slide, what to read first
is obvious without effort. No overlap and nothing off-page on any shown slide -- zero exceptions, not
"none noticed."

**0.6-0.79 (Good)**: No overlap and nothing off-page on any shown slide, and on every shown slide a
viewer can immediately tell what to read first (both are disqualifying per the gating rule above, not
a deduction, if violated even once). Within that constraint: minor misalignments or an occasionally
cramped slide.

**0.4-0.59 (Fair)**: Uneven spacing or alignment in several places, or muddled hierarchy that makes a
viewer hesitate over what to read first; layout is serviceable but visibly unpolished. Also lands
here -- regardless of how clean everything else is -- when a single small overlap, off-page element,
or "what do I read first" lapse appears on an otherwise strong deck.

**0.2-0.39 (Poor)**: Frequent clutter, element collisions, confusing placement, or competing focal
points that hurt readability.

**0.0-0.19 (Critical)**: Layout is broken -- severe overlap, text running off the page or clipped,
or no discernible reading order or hierarchy.

Evaluate step by step: (1) check EVERY slide for overlap and out-of-bounds/clipped elements FIRST --
any single instance caps the score at Fair or below regardless of later findings; (2) on every slide,
check whether a viewer would immediately know what to read first -- treat any slide that fails this
the same as an overlap, capping the score at Fair or below; (3) assess
alignment, margins, and spacing consistency; (4) assess balance and whitespace; (5) assess reading
order and typographic hierarchy across slides; (6) give a final score and an explanation naming the
slides where problems occur.
""" + _FORMAT_COMPLIANCE_TEMPLATE

_template_adherence_judge = make_visual_comparison_judge(
    system_prompt=_TEMPLATE_ADHERENCE_SYSTEM_PROMPT, max_content_tokens=100_000)
_layout_quality_judge = make_visual_quality_judge(system_prompt=_LAYOUT_QUALITY_SYSTEM_PROMPT)


def template_adherence(*, data_point_id: str, conversation: list[ConversationTurn],
                       expected_output: ArtifactAgentOutput | None = None,
                       eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """Renders the query's template.pptx and the produced output deck (each trimmed to their first
    4 slides) to PDF and judges whether the output visually adopted the template's color/font/
    layout theme. `None` if either file is missing, the trim fails, rendering fails (e.g. no
    LibreOffice on this host), or the model lacks native PDF support -- an infra/capability gap, not
    a 0 score.

    A deck longer than 4 slides is scored from its first 4; that is recorded in
    `metadata["degraded"]` per `helpers/files.py`'s "truncate, record it, never silent" rule.

    Does not forward the per-sample `eval_rubric` -- see the module docstring:
    `expectations_satisfaction` is this task's only reader of that channel."""
    del data_point_id, expected_output, eval_rubric, kwargs
    template_file = _template_file(conversation)
    output_file = _pptx_file(conversation)
    if template_file is None or output_file is None:
        return None, {"applicable": False, "reason": "missing template.pptx or output .pptx"}
    template_trimmed, t_trim_reason, t_degraded = _first_n_slides(template_file.file_data)
    output_trimmed, o_trim_reason, o_degraded = _first_n_slides(output_file.file_data)
    if template_trimmed is None or output_trimmed is None:
        return None, {"applicable": False,
                      "reason": f"trim failed: template={t_trim_reason or 'ok'}, output={o_trim_reason or 'ok'}"}
    template_pdf, t_reason = render_pptx_to_pdf(template_trimmed)
    output_pdf, o_reason = render_pptx_to_pdf(output_trimmed)
    if template_pdf is None or output_pdf is None:
        return None, {"applicable": False,
                      "reason": f"rendering failed: template={t_reason or 'ok'}, output={o_reason or 'ok'}"}
    score, metadata = _template_adherence_judge(template_pdf, output_pdf, None)
    # Append rather than overwrite: the judge may have degraded its own payload, and a score degraded
    # twice must report both causes.
    trimmed = []
    if t_degraded:
        trimmed.append(f"template: {t_degraded}")
    if o_degraded:
        trimmed.append(f"output: {o_degraded}")
    if trimmed:
        metadata = dict(metadata)
        existing = metadata.get("degraded")
        metadata["degraded"] = "; ".join([existing, *trimmed] if existing else trimmed)
    return score, metadata


def layout_quality(*, data_point_id: str, conversation: list[ConversationTurn],
                   expected_output: ArtifactAgentOutput | None = None,
                   eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """Renders the produced output deck (trimmed to its first 4 slides) to PDF and judges layout:
    element bounds and overlap, alignment and margins, balance and whitespace, reading order, and
    typographic hierarchy. `None` if the output pptx is missing, the trim fails, rendering fails, or
    the model lacks native PDF support -- an infra/capability gap, not a 0 score.

    A deck longer than 4 slides is scored from its first 4; that is recorded in
    `metadata["degraded"]` per `helpers/files.py`'s "truncate, record it, never silent" rule.

    Does not forward the per-sample `eval_rubric` -- see the module docstring:
    `expectations_satisfaction` is this task's only reader of that channel."""
    del data_point_id, expected_output, eval_rubric, kwargs
    output_file = _pptx_file(conversation)
    if output_file is None:
        return None, {"applicable": False, "reason": "no output .pptx in the response"}
    output_trimmed, trim_reason, degraded = _first_n_slides(output_file.file_data)
    if output_trimmed is None:
        return None, {"applicable": False, "reason": f"trim failed: {trim_reason}"}
    output_pdf, reason = render_pptx_to_pdf(output_trimmed)
    if output_pdf is None:
        return None, {"applicable": False, "reason": f"rendering failed: {reason}"}
    score, metadata = _layout_quality_judge(output_pdf, None)
    # Append rather than overwrite: the judge may already have degraded its own payload.
    if degraded:
        metadata = dict(metadata)
        existing = metadata.get("degraded")
        metadata["degraded"] = f"{existing}; {degraded}" if existing else degraded
    return score, metadata

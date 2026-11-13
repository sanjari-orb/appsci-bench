"""File-content judges -- Level 2 of the judge composition.

This module is the one place in the package that reads `file_data` -- Level 1 (`transcript.py`) is
deliberately blind to it. It exposes three factories:
  * `make_file_similarity_judge` compares the produced deck against the gold `expected_output.files`
    for FUNCTIONAL equivalence (output-vs-output).
  * `make_source_grounding_judge` reads the *query's* input source files and checks the produced
    output (response json/text) against them (source-vs-output groundedness). Note this grounding is
    performed based on LLM judge only, no other tool is used for deeper validations.
  * `make_source_coverage_judge` reads the same inputs but asks the opposite question: what
    PROPORTION of the source material the output covers (source-vs-output coverage / omission). It
    and the grounding judge share one internal builder (`_make_source_judge`).
Plus two vision-only factories that judge already-rendered document `File`s (e.g. a pptx rendered
to PDF by `helpers/render.py`) instead of the raw source/output files above:
  * `make_visual_comparison_judge` compares two rendered documents (reference vs. actual) visually.
  * `make_visual_quality_judge` scores one rendered document standalone, no reference.
Both bail to `None` on overflow or missing model capability rather than degrading -- native
document/image blocks can't be truncated, and there is no lower-fidelity fallback to send instead.
All share the content machinery below. "Guard, not limit": the default posture is full-fidelity
content every time (native document/image blocks where the model supports them, full text extraction
otherwise -- never a summary or a sample); a size guard only kicks in when content actually overflows.
`make_file_similarity_judge` and `make_source_coverage_judge` degrade the payload on overflow
(truncate, record it under metadata["degraded"], never silent) -- safe because they compare
renderings / measure omission. `make_source_grounding_judge` hard-bails to `None` instead --
truncating financial/source content risks silently dropping the very facts groundedness is checking,
which would corrupt the judgment rather than merely losing some coverage; the bail-out is recorded
via `reason` in metadata.
"""
from __future__ import annotations

import base64
import io
from typing import Any, Callable, Literal

import litellm
from pydantic import BaseModel

from appsci_bench.common.model_config import ModelConfig, config_for
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn, File

from .llm import JudgeVerdict, llm_judge_call

_DEFAULT_SYSTEM_PROMPT = (
    "You are comparing two files (or file sets) produced for the SAME task. They may differ in "
    "structure, formatting, slide/section count, or even file format -- judge whether they "
    "accomplish the same task and convey the same substantive content and outcome, NOT whether "
    "they are structurally or byte-for-byte identical. Two very differently structured files that "
    "deliver the same substance should score highly; two similarly structured files that differ in "
    "substance should not."
)

_INTRO_TEXT = (
    "Below are an ACTUAL file (produced by the agent under test) and an EXPECTED file (the ground "
    "truth) for the same data point, followed by any evaluation rubric. Compare them for "
    "functional equivalence per the instructions above."
)

# A conservative chars-per-token estimate. This only feeds the size GUARD (deciding whether to
# degrade), not a precision budget -- being roughly right is enough for its job.
_CHARS_PER_TOKEN_ESTIMATE = 4
DEFAULT_MAX_CONTENT_TOKENS = 50_000
# Coverage's degrade path bails to None below this fraction of surviving source text: when the output
# nearly fills the budget, the source can be truncated to a sliver, and a coverage score against source
# the judge barely saw misrepresents what was actually measured.
_MIN_SOURCE_SURVIVAL = 0.5

_MIME_TYPES = {
    "pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png",
}

_NATIVE_DOCUMENT_TYPES = {"pdf"}
_NATIVE_IMAGE_TYPES = {"jpg", "png"}


def extract_pptx_text(data: bytes) -> str:
    """Full text extraction: every slide's every text-bearing shape, plus speaker notes. No
    sampling, no slide limit."""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    lines: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        lines.append(f"--- Slide {i} ---")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text
            if notes.strip():
                lines.append(f"[notes] {notes}")
    return "\n".join(lines)


def extract_docx_text(data: bytes) -> str:
    """Full text extraction: every paragraph, in order. No sampling."""
    from docx import Document

    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def extract_xlsx_text(data: bytes) -> str:
    """Full text extraction: every sheet, every row. No sampling, no row/column-count summary."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    lines: list[str] = []
    for sheet in wb.worksheets:
        lines.append(f"--- Sheet: {sheet.title} ---")
        for row in sheet.iter_rows(values_only=True):
            if any(v is not None for v in row):
                lines.append(", ".join("" if v is None else str(v) for v in row))
    return "\n".join(lines)


def extract_plain_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def extract_pdf_text(data: bytes) -> str:
    """Full text extraction: every page, in order. No sampling, no page limit.

    Uses pypdf (BSD-licensed) rather than PyMuPDF/fitz (AGPL, not usable commercially here).
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    lines: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        lines.append(f"--- Page {i} ---")
        text = page.extract_text()
        if text.strip():
            lines.append(text)
    return "\n".join(lines)


_DEFAULT_TEXT_EXTRACTORS: dict[str, Callable[[bytes], str]] = {
    "pptx": extract_pptx_text,
    "docx": extract_docx_text,
    "xlsx": extract_xlsx_text,
    "csv": extract_plain_text,
    "txt": extract_plain_text,
    "md": extract_plain_text,
    "pdf": extract_pdf_text,
}


def _supports(checker: Callable[[str, str | None], bool], model: str) -> bool:
    """Query LiteLLM's model-capability database, defaulting to False (safe: falls through to
    text extraction / skip) if the model is unrecognized or the check itself errors -- a capability
    lookup failing is not the same as the model actually supporting the capability."""
    try:
        return bool(checker(model, None))
    except Exception:
        return False


def _b64_data_uri(file: File) -> str:
    return f"data:{_MIME_TYPES[file.file_type]};base64,{base64.b64encode(file.file_data).decode('ascii')}"


def _native_image_block(file: File) -> dict:
    return {"type": "image_url", "image_url": {"url": _b64_data_uri(file)}}


def _native_document_block(file: File) -> dict:
    return {"type": "file", "file": {"file_data": _b64_data_uri(file), "filename": file.file_name}}


def _file_content(
    file: File, *, model: str, extractors: dict[str, Callable[[bytes], str]],
    allow_native_pdf: bool = False,
) -> tuple[list[dict], str]:
    """Return (content_blocks, how) for one file: a native document/image block, extracted text
    (as a `{"type": "text", ...}` block), or empty with a `how` explaining why nothing was sent
    (e.g. no vision support and no extractor for this file_type) -- never silent.

    `allow_native_pdf` gates whether a PDF is sent as a native (unextracted) document block instead
    of extracted text. It defaults to False -- text extraction -- because a PDF here is normally a
    grounding/source document being judged for its CONTENT, and native PDF blocks are large (the
    full file, base64-encoded) and not decomposable by `_degrade`'s size guard, so a single sizable
    source PDF can blow the whole judge token budget on its own regardless of anything else in the
    call. Pass True only when the PDF itself IS the thing being visually judged (e.g. a rendered
    slide deck from `helpers/render.py`), where there is no meaningful text to extract instead.
    """
    if (
        allow_native_pdf
        and file.file_type in _NATIVE_DOCUMENT_TYPES
        and _supports(litellm.utils.supports_pdf_input, model)
    ):
        return [_native_document_block(file)], "native_document"
    if file.file_type in _NATIVE_IMAGE_TYPES and _supports(litellm.utils.supports_vision, model):
        return [_native_image_block(file)], "native_image"
    extractor = extractors.get(file.file_type)
    if extractor is not None:
        return [{"type": "text", "text": extractor(file.file_data)}], "text_extraction"
    if file.file_type in _NATIVE_IMAGE_TYPES:
        return [], "skipped_no_vision_support"
    if allow_native_pdf and file.file_type in _NATIVE_DOCUMENT_TYPES:
        return [], "skipped_no_native_pdf_support"
    return [], "skipped_no_extractor"


def _pair_files(
    actual: list[File], expected: list[File],
) -> tuple[list[tuple[str, File | None, File | None]], list[str]]:
    """Pair actual vs expected files: match by `file_name` first, else by position.
    Returns (pairs, unmatched_labels) -- an unpaired file is flagged, never silently dropped."""
    actual_by_name = {f.file_name: f for f in actual}
    expected_by_name = {f.file_name: f for f in expected}
    matched_names = set(actual_by_name) & set(expected_by_name)

    pairs: list[tuple[str, File | None, File | None]] = [
        (name, actual_by_name[name], expected_by_name[name]) for name in sorted(matched_names)
    ]

    remaining_actual = [f for f in actual if f.file_name not in matched_names]
    remaining_expected = [f for f in expected if f.file_name not in matched_names]
    for i, (a, e) in enumerate(zip(remaining_actual, remaining_expected)):
        pairs.append((f"position_{i}", a, e))

    unmatched: list[str] = []
    leftover_actual = remaining_actual[len(remaining_expected):]
    leftover_expected = remaining_expected[len(remaining_actual):]
    unmatched += [f"actual:{f.file_name} (no matching expected file)" for f in leftover_actual]
    unmatched += [f"expected:{f.file_name} (no matching actual file)" for f in leftover_expected]
    return pairs, unmatched


def _estimate_tokens(content_blocks: list[dict]) -> int:
    total_chars = 0
    for b in content_blocks:
        if b["type"] == "text":
            total_chars += len(b["text"])
        elif b["type"] == "file":
            total_chars += len(b["file"]["file_data"])
        elif b["type"] == "image_url":
            total_chars += len(b["image_url"]["url"])
    return total_chars // _CHARS_PER_TOKEN_ESTIMATE


def _text_chars(content_blocks: list[dict]) -> int:
    """Total characters in the extracted-TEXT blocks (the only blocks `_degrade` can truncate)."""
    return sum(len(b["text"]) for b in content_blocks if b["type"] == "text")


def _degrade(content_blocks: list[dict], max_content_tokens: int) -> tuple[list[dict], str]:
    """The guard's fallback: proportionally truncate extracted TEXT blocks to fit the budget.
    Native document/image blocks are never truncated (they aren't decomposable) -- if native
    content alone exceeds the guard, that is reported rather than silently ignored.
    """
    budget_chars = max_content_tokens * _CHARS_PER_TOKEN_ESTIMATE
    native_chars = sum(
        len(b["file"]["file_data"]) if b["type"] == "file"
        else len(b["image_url"]["url"]) if b["type"] == "image_url" else 0
        for b in content_blocks)
    text_chars = _text_chars(content_blocks)
    remaining_budget = max(0, budget_chars - native_chars)

    if text_chars == 0 or remaining_budget >= text_chars:
        return content_blocks, (
            f"native document/image content alone (~{native_chars // _CHARS_PER_TOKEN_ESTIMATE} "
            f"est. tokens) exceeds max_content_tokens={max_content_tokens}; cannot truncate further")

    ratio = remaining_budget / text_chars
    degraded = [
        {"type": "text", "text": b["text"][: max(0, int(len(b["text"]) * ratio))]}
        if b["type"] == "text" else b
        for b in content_blocks
    ]
    return degraded, f"extracted text truncated to fit max_content_tokens={max_content_tokens} estimate"


def make_file_similarity_judge(
    *, extractors: dict[str, Callable[[bytes], str]] | None = None,
    max_content_tokens: int = DEFAULT_MAX_CONTENT_TOKENS,
    output_schema: type[BaseModel] = JudgeVerdict,
    config: ModelConfig | None = None,
) -> Callable[..., tuple[float, dict]]:
    """Build a ready `eval_function_path` target comparing `expected_output.files` against the
    last turn's actual `response.files` for FUNCTIONAL equivalence (not structural/byte diff).

    `extractors` overrides/extends the default per-file-type text extractors (pptx/docx/xlsx/
    csv/txt/md/pdf); jpg/png prefer a native image content block when the configured model
    supports it (LiteLLM's capability database), never a text extraction for those types in
    this version. `max_content_tokens` is the size GUARD (see
    `_degrade`), not a default limiter: it only trims when the full-fidelity payload would
    actually overflow it, and metadata["degraded"] records that when it happens.
    """
    merged_extractors = {**_DEFAULT_TEXT_EXTRACTORS, **(extractors or {})}

    def eval_function(
        *, data_point_id: str, conversation: list[ConversationTurn],
        expected_output: ArtifactAgentOutput | None = None, eval_rubric: str | None = None,
        **kwargs: Any,
    ) -> tuple[float, dict]:
        del data_point_id, kwargs  # unused -- part of the keyword-only contract
        cfg = config or config_for("JUDGE_MODEL")
        expected_files = expected_output.files if expected_output is not None else []
        actual_files = conversation[-1].response.files if conversation else []
        if not expected_files and not actual_files:
            return 0.0, {"error": "no files on either side to compare"}

        pairs, unmatched = _pair_files(actual_files, expected_files)
        content_blocks: list[dict] = [{"type": "text", "text": _INTRO_TEXT}]
        per_file_how: dict[str, str] = {}
        for label, actual_f, expected_f in pairs:
            for tag, f in (("ACTUAL", actual_f), ("EXPECTED", expected_f)):
                if f is None:
                    continue
                blocks, how = _file_content(f, model=cfg.model, extractors=merged_extractors)
                per_file_how[f"{label}:{tag}"] = how
                content_blocks.append(
                    {"type": "text", "text": f"\n=== {tag} file: {f.file_name} ({how}) ==="})
                content_blocks.extend(blocks)

        degraded: str | None = None
        if _estimate_tokens(content_blocks) > max_content_tokens:
            content_blocks, degraded = _degrade(content_blocks, max_content_tokens)

        if eval_rubric:
            content_blocks.append({"type": "text", "text": f"\nRubric: {eval_rubric}"})

        verdict = llm_judge_call(
            system_prompt=_DEFAULT_SYSTEM_PROMPT, user_prompt=content_blocks,
            output_schema=output_schema, config=cfg)
        metadata = verdict.model_dump(exclude={"score"})
        metadata["per_file"] = per_file_how
        if unmatched:
            metadata["unmatched_files"] = unmatched
        if degraded:
            metadata["degraded"] = degraded
        return verdict.score, metadata

    return eval_function


_GROUNDING_SYSTEM_PROMPT = (
    "You are checking whether the OUTPUT produced by an agent is GROUNDED in the SOURCE materials "
    "that were provided as input to the agent. Every figure, metric, statement, and claim in the "
    "OUTPUT must be either directly present in the SOURCE, correctly derived/computed from it (e.g. "
    "a variance or total the arithmetic supports), or clearly framed as an assumption. Flag any "
    "fabricated fact, invented number, or conclusion the SOURCE does not support, and any incorrect "
    "computation. Do NOT reward mere topical similarity to the source, and do NOT penalize the "
    "output for omitting source content (that is a coverage question, not a groundedness one). "
    "Score 1.0 only when the output introduces no ungrounded content and all computations are "
    "correct; score low when the output invents numbers or contradicts the source.\n\n"
)

_GROUNDING_INTRO_TEXT = (
    "Below are the SOURCE materials (the factual inputs given to the agent) followed by the OUTPUT "
    "the agent produced, and then any per-sample eval_rubric. Judge the output's groundedness against "
    "the SOURCE per the instructions above." )

_COVERAGE_SYSTEM_PROMPT = (
    "You are checking how COMPLETELY the OUTPUT produced by an agent COVERS the material content of "
    "the SOURCE materials that were provided as input to the agent, IN LIGHT OF the user's REQUEST "
    "(if one is shown). Enumerate the material facts, figures, metrics, data points, and named "
    "entities present in the SOURCE, then judge what PROPORTION of them is faithfully captured in "
    "the OUTPUT. A fact the REQUEST explicitly excluded, or that falls outside a scope/focus the "
    "REQUEST stated (e.g. \"focus on X\", \"just cover Y\", \"skip Z\"), is NOT material for this "
    "judgment even if it is prominent in the SOURCE -- coverage is scored against what the user "
    "actually asked the OUTPUT to capture, not against the SOURCE's full content regardless of "
    "request. When the REQUEST states no such scoping, judge coverage against the SOURCE as a "
    "whole. Score by coverage: 1.0 only when no significant IN-SCOPE source content is omitted "
    "(every material fact appears, faithfully represented, not paraphrased into inaccuracy); score "
    "lower as more material in-scope source content is silently dropped. Do NOT penalize the OUTPUT "
    "for adding extra content beyond the source, and do NOT reward the OUTPUT for merely being "
    "grounded / free of fabrication -- whether invented content appears is a groundedness question, "
    "not a coverage one. Judge omission, not invention.\n\n"
)

_COVERAGE_INTRO_TEXT = (
    "Below are the SOURCE materials (the factual inputs given to the agent), followed by the user's "
    "REQUEST (if any -- read it to scope what counts as material before enumerating source facts), "
    "the OUTPUT the agent produced, and then any per-sample eval_rubric. Judge how completely the "
    "OUTPUT covers the SOURCE per the instructions above." )


def _make_source_judge(
    *, system_prompt_base: str, intro_text: str, on_overflow: Literal["bail", "degrade"],
    extractors: dict[str, Callable[[bytes], str]] | None = None,
    max_content_tokens: int = DEFAULT_MAX_CONTENT_TOKENS,
    exclude_name_substrings: tuple[str, ...] = (),
    output_schema: type[BaseModel] = JudgeVerdict,
    config: ModelConfig | None = None,
    metric_rubric: str | None = None,
    include_query_text: bool = False,
) -> Callable[..., tuple[float | None, dict]]:
    """Shared machinery for the source-vs-output judges (`make_source_grounding_judge`,
    `make_source_coverage_judge`). Reads the *query's* attached source files (excluding any whose
    `file_name` matches `exclude_name_substrings`, case-insensitive) plus the produced OUTPUT
    (response `json`, else `text`) into one multimodal judge call.

    `include_query_text`, if set, also sends the query's `text` (the user's request/brief) as a
    labelled REQUEST block. It is placed alongside the OUTPUT in the never-truncated tail of the
    payload (see the overflow handling below) rather than with SOURCE, so a large source that gets
    degraded on overflow can never drag the brief down with it -- exactly the case where the brief
    matters most for scoping what "coverage" should mean. Only `make_source_coverage_judge` passes
    this: coverage needs the brief to tell material source content apart from content the user
    scoped out, whereas grounding's fabrication check doesn't depend on what was asked.

    The two knobs that distinguish the public judges: `system_prompt_base`/`intro_text` frame the
    question (grounding vs. coverage), and `on_overflow` sets the size-guard policy when
    source+output exceeds `max_content_tokens`:
      * "bail"    -> return `None` (not applicable). Truncating would risk silently changing the
                     very facts being checked (groundedness).
      * "degrade" -> keep the full OUTPUT and `_degrade` only the SOURCE text into the budget left
                     over, keep scoring, and record it in `metadata["degraded"]` (coverage tolerates
                     partial-fidelity source). If the OUTPUT alone exceeds the budget, bail to `None`
                     -- truncating the output would manufacture the very omission coverage measures. Also
                     bails to `None` when fewer than `_MIN_SOURCE_SURVIVAL` of the source text
                     survives truncation (a coverage score against a gutted source would mislead).

    `metric_rubric`, if given, is the METRIC's own scoring definition, baked into the system prompt
    once here; it is NOT the per-sample `eval_rubric` (a per-data-point NL note appended per call).

    These judges are SOURCE-anchored: they score the OUTPUT against the *query's input source files*
    only. `expected_output` (the gold artifact, incl. any `required_content` checklist a sample
    declares) is deliberately NOT consulted -- a "must-cover X" checklist is a gold-vs-output question,
    not a source-vs-output one, and folding it in here would conflate two different metrics. A task
    needing a must-cover checklist should add a separate gold-anchored metric rather than expect these
    judges to honor `required_content`.
    """
    merged_extractors = {**_DEFAULT_TEXT_EXTRACTORS, **(extractors or {})}
    lowered_excludes = tuple(s.lower() for s in exclude_name_substrings)
    system_prompt = f"{system_prompt_base}\n\n{metric_rubric}" if metric_rubric else system_prompt_base

    def _is_excluded(file: File) -> bool:
        name = file.file_name.lower()
        return any(sub in name for sub in lowered_excludes)

    def eval_function(
        *, data_point_id: str, conversation: list[ConversationTurn],
        expected_output: ArtifactAgentOutput | None = None, eval_rubric: str | None = None,
        **kwargs: Any,
    ) -> tuple[float | None, dict]:
        # expected_output is intentionally unused: these judges are source-anchored (see the builder
        # docstring). Any `required_content` gold checklist is NOT honored here by design.
        del data_point_id, expected_output, kwargs  # unused -- part of the keyword-only contract
        cfg = config or config_for("JUDGE_MODEL")
        if not conversation:
            return None, {"applicable": False, "reason": "no conversation to judge"}

        query_files = conversation[-1].query.files
        source_files = [f for f in query_files if not _is_excluded(f)]
        excluded = [f.file_name for f in query_files if _is_excluded(f)]
        if not source_files:
            return None, {"applicable": False,
                          "reason": "no source files on the query to judge against",
                          "excluded_files": excluded}

        content_blocks: list[dict] = [{"type": "text", "text": intro_text}]
        per_file_how: dict[str, str] = {}

        content_blocks.append({"type": "text", "text": "\n=== SOURCE materials ==="})
        for f in source_files:
            blocks, how = _file_content(f, model=cfg.model, extractors=merged_extractors)
            per_file_how[f"SOURCE:{f.file_name}"] = how
            content_blocks.append({"type": "text", "text": f"\n--- SOURCE file: {f.file_name} ({how}) ---"})
            content_blocks.extend(blocks)

        output_start_idx = len(content_blocks)  # everything from here on is never truncated
        if include_query_text:
            query_text = conversation[-1].query.text
            if query_text and query_text.strip():
                content_blocks.append(
                    {"type": "text", "text": f"\n=== USER REQUEST (brief) ===\n{query_text}"})
        content_blocks.append({"type": "text", "text": "\n=== OUTPUT ==="})
        response = conversation[-1].response
        if response.json:
            per_file_how["OUTPUT"] = "response_json"
            content_blocks.append({"type": "text", "text": f"\n--- OUTPUT (JSON) ---\n{response.json}"})
        elif response.text:
            per_file_how["OUTPUT"] = "response_text"
            content_blocks.append({"type": "text", "text": f"\n--- OUTPUT (text) ---\n{response.text}"})
        else:
            return None, {"applicable": False, "reason": "no output (json/text) to judge",
                          "excluded_files": excluded}

        degraded: str | None = None
        if _estimate_tokens(content_blocks) > max_content_tokens:
            if on_overflow == "bail":
                # Hard bail-out, not degrade-and-continue: truncating financial/source content risks
                # silently changing the very facts groundedness is checking, unlike
                # make_file_similarity_judge where degrading a rendering-comparison input is safe.
                return None, {"applicable": False,
                              "reason": "source + output content exceeds max_content_tokens, cannot judge in full",
                              "excluded_files": excluded}
            elif on_overflow == "degrade":
                # Degrade the SOURCE only, never the OUTPUT (or the REQUEST brief, which sits in
                # the same never-truncated tail -- see include_query_text above): this judge
                # measures how much of the SOURCE the OUTPUT covers, so truncating the OUTPUT would
                # manufacture the very omission being scored, and the brief is what tells material
                # source content apart from content the user scoped out, so it must survive intact
                # too. Keep that tail full and truncate the source into the budget left over (a
                # dropped source tail lowers coverage, it doesn't corrupt the judgment the way a
                # silently-altered figure would corrupt groundedness). If the tail alone can't fit,
                # we can't load it in full -> bail to None (same reasoning as grounding).
                source_blocks = content_blocks[:output_start_idx]
                output_blocks = content_blocks[output_start_idx:]
                output_tokens = _estimate_tokens(output_blocks)
                if output_tokens >= max_content_tokens:
                    return None, {"applicable": False,
                                  "reason": "output (+ request brief) content alone exceeds "
                                            "max_content_tokens; cannot load the full output to "
                                            "judge coverage",
                                  "excluded_files": excluded}
                source_text_before = _text_chars(source_blocks)
                degraded_source, degraded = _degrade(source_blocks, max_content_tokens - output_tokens)
                # Guard against the near-cap case: if the output nearly fills the budget, the leftover
                # for source can be a sliver, truncating source text to almost nothing -- yet the LLM
                # judge would still return a real coverage score against source it barely saw. Below a
                # floor of surviving source, that score misrepresents what was actually judged, so bail
                # to None rather than report a misleading number. (source_text_before == 0 means the
                # source is all native document/image blocks, which _degrade never truncates -> no
                # misrepresentation, proceed.)
                source_text_after = _text_chars(degraded_source)
                survival = source_text_after / source_text_before if source_text_before else 1.0
                if survival < _MIN_SOURCE_SURVIVAL:
                    return None, {"applicable": False,
                                  "reason": f"only ~{survival:.0%} of source text survives truncation "
                                            f"into the budget left by the output "
                                            f"(< {_MIN_SOURCE_SURVIVAL:.0%} floor); coverage would "
                                            f"misrepresent what was judged",
                                  "source_survival": round(survival, 3),
                                  "excluded_files": excluded}
                degraded = f"{degraded}; ~{survival:.0%} of source text retained"
                content_blocks = degraded_source + output_blocks
            else:
                raise ValueError(f"unexpected on_overflow={on_overflow!r}")

        if eval_rubric:
            content_blocks.append({"type": "text", "text": f"\nRubric: {eval_rubric}"})

        verdict = llm_judge_call(
            system_prompt=system_prompt, user_prompt=content_blocks,
            output_schema=output_schema, config=cfg)
        metadata = verdict.model_dump(exclude={"score"})
        metadata["per_file"] = per_file_how
        if excluded:
            metadata["excluded_files"] = excluded
        if degraded:
            metadata["degraded"] = degraded
        return verdict.score, metadata

    return eval_function


def make_source_grounding_judge(
    *,extractors: dict[str, Callable[[bytes], str]] | None = None,
    max_content_tokens: int = DEFAULT_MAX_CONTENT_TOKENS,
    exclude_name_substrings: tuple[str, ...] = (),
    output_schema: type[BaseModel] = JudgeVerdict,
    config: ModelConfig | None = None,
    metric_rubric: str | None = None,
) -> Callable[..., tuple[float | None, dict]]:
    """Build a ready `eval_function_path` target judging whether the agent's OUTPUT is GROUNDED in the
    INPUT source materials -- unlike `make_file_similarity_judge` (which compares the produced output
    against the gold `expected_output.files`), this reads the *query's* attached source files (the
    facts the agent was given) and checks the produced output against them for fabrication /
    arithmetic errors. It reuses this module's file-content machinery (`_file_content`, extractors)
    but NOT `_degrade` -- see the module docstring for why this judge hard-bails on overflow instead
    of degrading. This helper is task-agnostic: the OUTPUT judged is the response `json` (else `text`),
    and any task-specific source exclusion is supplied via `exclude_name_substrings`.

    Source files are the last turn's `query.files`, EXCLUDING any whose `file_name` contains a
    substring in `exclude_name_substrings` (case-insensitive; default `()` excludes nothing).
    Exclusion is by NAME, not file_type -- a task can drop, e.g., a styling `template.pptx` while
    keeping genuine source files by passing `exclude_name_substrings=("template.pptx",)` at its
    call site (prefer the full styling-asset filename over a bare word fragment like `"template"`,
    which would also wrongly exclude a genuine source file merely named e.g. `Q1_template_review.pptx`).

    `metric_rubric`, if given, is the METRIC's own definition -- the task designer's scoring
    bands/criteria for what "grounded" means for this objective, same role as a `make_transcript_judge`
    caller's `prompt_template`. It's baked into the system prompt once at construction time; it is NOT
    the same channel as the per-call `eval_rubric`, which is a per-sample NL note
    (`EvalSample.eval_rubric`) -- exactly like `make_transcript_judge`'s `eval_rubric` template
    variable, it's purely additive context with nothing to conflict with, because the metric
    definition was never routed through that channel in the first place.

    Returns `None` (not applicable) when there are no source files to judge against, or no output
    to judge, or the source+output content is too large to judge in full (see module docstring --
    this judge hard-bails rather than degrading).
    """
    return _make_source_judge(
        system_prompt_base=_GROUNDING_SYSTEM_PROMPT, intro_text=_GROUNDING_INTRO_TEXT,
        on_overflow="bail", extractors=extractors, max_content_tokens=max_content_tokens,
        exclude_name_substrings=exclude_name_substrings, output_schema=output_schema,
        config=config, metric_rubric=metric_rubric)


def make_source_coverage_judge(
    *,extractors: dict[str, Callable[[bytes], str]] | None = None,
    max_content_tokens: int = DEFAULT_MAX_CONTENT_TOKENS,
    exclude_name_substrings: tuple[str, ...] = (),
    output_schema: type[BaseModel] = JudgeVerdict,
    config: ModelConfig | None = None,
    metric_rubric: str | None = None,
) -> Callable[..., tuple[float | None, dict]]:
    """Build a ready `eval_function_path` target judging how COMPLETELY the agent's OUTPUT COVERS the
    INPUT source materials -- the coverage counterpart to `make_source_grounding_judge`. It reads the
    same inputs (the *query's* attached source files, excluding any matching `exclude_name_substrings`;
    the produced OUTPUT as response `json` else `text`) and shares this module's file-content machinery,
    but asks the opposite question: grounding flags what the OUTPUT invented; coverage measures what
    proportion of the SOURCE the OUTPUT captured. It does NOT penalize the output for adding content,
    and does NOT reward mere absence of fabrication.

    Unlike grounding, this judge DEGRADES on overflow (truncates the extracted SOURCE text to fit
    the budget left after the full OUTPUT, recording it in `metadata["degraded"]`) rather than
    hard-bailing: a dropped source tail lowers the measured coverage but does not corrupt the
    judgment the way a silently altered figure would corrupt groundedness. The OUTPUT itself is
    never truncated -- doing so would manufacture the very omission coverage measures.

    Also sends the query's `text` (the user's request/brief) to the judge, labelled as the REQUEST,
    always full-fidelity (see `_make_source_judge`'s `include_query_text`). This matters most on a
    large source: the brief is what tells a MATERIAL fact the requester would flag as important
    apart from one the request itself scoped out (a stated focus, an explicit exclusion, "just cover
    X") -- without it, coverage would otherwise judge the deck against the whole source regardless of
    what was actually asked for.

    `exclude_name_substrings` and `metric_rubric` behave exactly as in `make_source_grounding_judge`.
    Returns `None` (not applicable) when there are no source files or no output to judge, or when the
    OUTPUT (plus the request brief) alone exceeds `max_content_tokens` (it cannot be loaded in full
    without truncation).
    """
    return _make_source_judge(
        system_prompt_base=_COVERAGE_SYSTEM_PROMPT, intro_text=_COVERAGE_INTRO_TEXT,
        on_overflow="degrade", extractors=extractors, max_content_tokens=max_content_tokens,
        exclude_name_substrings=exclude_name_substrings, output_schema=output_schema,
        config=config, metric_rubric=metric_rubric, include_query_text=True)


def _visual_block_or_none(file: File, *, model: str) -> tuple[list[dict], str] | None:
    """`_file_content` for an already-rendered document/image File, but treats "no native block
    could be built" (e.g. the model lacks PDF/vision support) as a hard failure rather than
    silently falling through to text extraction -- there is no meaningful text extraction for a
    rendered PDF/image, so an empty block list here means the judge has nothing to see."""
    blocks, how = _file_content(file, model=model, extractors={}, allow_native_pdf=True)
    if not blocks:
        return None
    return blocks, how


def make_visual_comparison_judge(
    *, system_prompt: str, max_content_tokens: int = DEFAULT_MAX_CONTENT_TOKENS,
    output_schema: type[BaseModel] = JudgeVerdict, config: ModelConfig | None = None,
) -> Callable[[File, File, str | None], tuple[float | None, dict]]:
    """Build a callable(reference_file, actual_file, eval_rubric) -> (score, metadata) comparing
    two already-rendered documents (e.g. PDFs from `helpers/render.py`) for visual similarity per
    `system_prompt`. Unlike the source/output judges above, this factory does NOT have the
    `eval_function` `(*, data_point_id, conversation, ...)` contract -- producing the two documents
    to compare requires task-specific rendering/selection logic (which file counts as "reference"
    vs. "actual", whether either needs trimming first) that this module has no business owning;
    the caller renders and passes the two `File`s in.

    Bails to `(None, {...})` when either file can't be sent as a native document/image block (the
    configured model lacks the relevant capability) or when the combined content would exceed
    `max_content_tokens` -- native blocks can't be truncated, so there is no degrade path here."""

    def judge(reference_file: File, actual_file: File, eval_rubric: str | None = None) -> tuple[float | None, dict]:
        cfg = config or config_for("JUDGE_MODEL")
        content_blocks: list[dict] = []

        ref = _visual_block_or_none(reference_file, model=cfg.model)
        if ref is None:
            return None, {"applicable": False,
                          "reason": f"model {cfg.model} cannot render {reference_file.file_type} natively"}
        content_blocks.append({"type": "text", "text": "\n=== REFERENCE ==="})
        content_blocks.extend(ref[0])

        actual = _visual_block_or_none(actual_file, model=cfg.model)
        if actual is None:
            return None, {"applicable": False,
                          "reason": f"model {cfg.model} cannot render {actual_file.file_type} natively"}
        content_blocks.append({"type": "text", "text": "\n=== ACTUAL ==="})
        content_blocks.extend(actual[0])

        if _estimate_tokens(content_blocks) > max_content_tokens:
            return None, {"applicable": False,
                          "reason": "rendered content alone exceeds max_content_tokens; cannot truncate native blocks"}

        if eval_rubric:
            content_blocks.append({"type": "text", "text": f"\nRubric: {eval_rubric}"})

        verdict = llm_judge_call(
            system_prompt=system_prompt, user_prompt=content_blocks,
            output_schema=output_schema, config=cfg)
        return verdict.score, verdict.model_dump(exclude={"score"})

    return judge


def make_visual_quality_judge(
    *, system_prompt: str, max_content_tokens: int = DEFAULT_MAX_CONTENT_TOKENS,
    output_schema: type[BaseModel] = JudgeVerdict, config: ModelConfig | None = None,
) -> Callable[[File, str | None], tuple[float | None, dict]]:
    """Build a callable(rendered_file, eval_rubric) -> (score, metadata) scoring one already-
    rendered document/image standalone (no reference) per `system_prompt`. Same non-`eval_function`
    shape and overflow/capability-gating bail policy as `make_visual_comparison_judge` -- see its
    docstring for why."""

    def judge(rendered_file: File, eval_rubric: str | None = None) -> tuple[float | None, dict]:
        cfg = config or config_for("JUDGE_MODEL")
        block = _visual_block_or_none(rendered_file, model=cfg.model)
        if block is None:
            return None, {"applicable": False,
                          "reason": f"model {cfg.model} cannot render {rendered_file.file_type} natively"}
        content_blocks = list(block[0])

        if _estimate_tokens(content_blocks) > max_content_tokens:
            return None, {"applicable": False,
                          "reason": "rendered content alone exceeds max_content_tokens; cannot truncate native blocks"}

        if eval_rubric:
            content_blocks.append({"type": "text", "text": f"\nRubric: {eval_rubric}"})

        verdict = llm_judge_call(
            system_prompt=system_prompt, user_prompt=content_blocks,
            output_schema=output_schema, config=cfg)
        return verdict.score, verdict.model_dump(exclude={"score"})

    return judge

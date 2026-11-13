"""Eval-function authoring helpers.

Orchestration only ever imports and calls an `eval_function_path` -- it has zero opinion
on how that function computes its score. This package is the shared toolbox task designers reach
for instead of re-deriving it per task: deterministic comparison helpers (no LLM, `deterministic.py`)
and LLM-judge helpers in increasing levels of composition -- `llm.py`'s generic call is Level 0,
`transcript.py`'s conversation judge is Level 1 (`judges/` holds its prebuilt library),
`files.py`'s file-content judges are Level 2. `files.py` also holds the two vision judges
(`make_visual_comparison_judge`, `make_visual_quality_judge`), which score a rendered image of a
produced file; `render.py` does that rendering.

See docs/evaluator_system_design.md (Task-Specific Evaluation Function) for the eval-function
contract these helpers plug into.
"""
from .deterministic import (
    json_field_match, make_json_field_eval_function, parse_bool, resolve_scalar, text_similarity,
)
from .files import (
    DEFAULT_MAX_CONTENT_TOKENS, make_file_similarity_judge, make_source_coverage_judge,
    make_source_grounding_judge, make_visual_comparison_judge, make_visual_quality_judge,
)
from .llm import JudgeParseError, JudgeVerdict, llm_judge_call
from .transcript import make_transcript_judge, render_conversation_for_judge

__all__ = [
    "resolve_scalar", "parse_bool", "text_similarity", "json_field_match", "make_json_field_eval_function",
    "JudgeVerdict", "JudgeParseError", "llm_judge_call",
    "make_transcript_judge", "render_conversation_for_judge",
    "make_file_similarity_judge", "make_source_grounding_judge", "make_source_coverage_judge",
    "make_visual_comparison_judge", "make_visual_quality_judge",
    "DEFAULT_MAX_CONTENT_TOKENS",
]

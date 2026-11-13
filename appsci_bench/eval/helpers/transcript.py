"""Transcript judge -- Level 1 of the judge composition.

`make_transcript_judge` wraps `llm_judge_call` (Level 0) into a ready `eval_function_path` target
that judges the CONVERSATION ITSELF -- what was said, in text/json, against a rubric and/or
ground truth. It is deliberately blind to attached-file CONTENT: that is Level 2's job
(`files.py`'s `make_file_similarity_judge`). This is a scope boundary, not a privacy or
context-size rule borrowed from the artifact agent user sim's `sim_view()` -- a metric needing
both "was the conversation good" and "was the produced file good" is two objectives (one per
level), not one judge doing two jobs.
"""
from __future__ import annotations

import pathlib
from typing import Any, Callable

from jinja2 import Template
from pydantic import BaseModel

from appsci_bench.common.model_config import ModelConfig
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn

from .llm import JudgeVerdict, llm_judge_call

_DEFAULT_SYSTEM_PROMPT = (
    "You are an impartial evaluator scoring an AI agent's conversation transcript. Read the "
    "transcript and the evaluation rubric / ground truth carefully, reason step by step, then "
    "output your verdict as a single JSON object matching the requested schema. Never invent "
    "facts that are not present in the transcript or the provided ground truth."
)


def render_conversation_for_judge(conversation: list[ConversationTurn]) -> str:
    """Render a conversation as judge-readable TEXT ONLY.

    Per turn: the query's and response's `text`/`json` verbatim, except that `json` is omitted when
    it merely repeats what `text` already contains (see `_render_io`); for any attached `files`, only
    that a file exists plus its `file_type`/`file_name` -- NEVER `file_data`. This function is
    what enforces Level 1's scope boundary by construction (files are structurally excluded from
    the rendered string), not merely by a convention callers have to remember.
    """
    lines: list[str] = []
    for i, turn in enumerate(conversation, start=1):
        lines.append(f"--- Turn {i} ---")
        lines.append(f"User query:\n{_render_io(turn.query)}")
        lines.append(f"Agent response:\n{_render_io(turn.response)}")
    return "\n\n".join(lines)


def _render_io(io: ArtifactAgentQuery | ArtifactAgentOutput) -> str:
    """Render one query/response's text/json + file EXISTENCE (never file_data). Both
    `ArtifactAgentQuery` and `ArtifactAgentOutput` carry the same text/json/files shape.

    The `json` line is DROPPED when that object already appears verbatim in `text`, so a judge is
    never shown the same payload twice. This is not cosmetic: over the completions transport the two
    slots are filled from one channel (`artifact_client`'s `HttpArtifactClient` puts the reply in
    `text` and, when the whole reply is an object, the same bytes in `json`), so rendering both would
    duplicate the ENTIRE reply in every judge prompt -- inflating it, and giving a judge two copies
    to weigh where the artifact said one thing. Dropping it also keeps the rendering identical to
    what judges saw before that slot was ever filled, so scores stay comparable across the change.

    Both lines still render when the slots carry DIFFERENT things -- a dataset query that sets text
    and json independently, or a transport with two real channels -- since then the structured half
    is information the text does not already carry."""
    parts: list[str] = []
    if io.text:
        parts.append(f"text: {io.text}")
    if io.json and io.json.strip() not in (io.text or ""):
        parts.append(f"json: {io.json}")
    for f in io.files:
        parts.append(f"[a file was attached: {f.file_name} ({f.file_type})]")
    return "\n".join(parts) if parts else "(empty)"


def _load_template(prompt_template: str) -> Template:
    """`prompt_template` may be literal jinja2 source OR a filesystem path to a .jinja2 file --
    an unmigrated task's existing jinja2 files
    (e.g. `slides_generator/eval_criteria/*.jinja2`) can be pointed at directly by path rather than
    read into a string first; a migrated task (e.g. `order_to_cash/eval.py`) more often inlines the
    template as a module-level string, since it's now colocated with the eval function using it.

    A multiline/long template STRING is not a plausible path -- `Path.is_file()` on one raises
    `OSError` ("File name too long") on some platforms rather than returning False, so we only even
    attempt the filesystem check for something path-shaped (single line, under a normal path length).
    """
    if "\n" not in prompt_template and len(prompt_template) < 4096:
        try:
            path = pathlib.Path(prompt_template)
            if path.is_file():
                return Template(path.read_text())
        except OSError:
            pass  # not a real path on this filesystem -- fall through to literal template source
    return Template(prompt_template)


def make_transcript_judge(
    prompt_template: str, *,
    output_schema: type[BaseModel] = JudgeVerdict,
    config: ModelConfig | None = None,
) -> Callable[..., tuple[float, dict]]:
    """Build a ready `eval_function_path` target: renders the conversation + rubric + expected
    output into `prompt_template`, calls `llm_judge_call`, returns `(score, metadata)`.

    Template variables available to `prompt_template`:
      transcript       -- the rendered conversation (see `render_conversation_for_judge`).
      eval_rubric       -- the sample's `eval_rubric` string, or None. This is a per-DATA-POINT NL
                           note (see `EvalSample.eval_rubric`'s docstring), NOT where the metric's own
                           scoring criteria goes -- that's `prompt_template` itself, authored once by
                           the task designer and fixed for every sample. Never make `eval_rubric`
                           mean two different things by, e.g., defaulting it to a task-wide string
                           when the sample doesn't set one.
      expected_text     -- `expected_output.text`, or None.
      expected_json     -- `expected_output.json`, or None.
    A template that references a variable that is None simply renders the empty string for it
    (standard jinja2 behavior) -- write templates defensively if a field is commonly absent.

    `metadata` is `output_schema`'s fields minus `score` (e.g. `reasoning` and any extra fields a
    custom schema declares), so a judge's chain-of-thought is always recorded, never discarded.
    """
    template = _load_template(prompt_template)

    def eval_function(
        *, data_point_id: str, conversation: list[ConversationTurn],
        expected_output: ArtifactAgentOutput | None = None, eval_rubric: str | None = None,
        **kwargs: Any,
    ) -> tuple[float, dict]:
        del data_point_id, kwargs  # unused -- part of the keyword-only contract
        user_prompt = template.render(
            transcript=render_conversation_for_judge(conversation),
            eval_rubric=eval_rubric,
            expected_text=expected_output.text if expected_output else None,
            expected_json=expected_output.json if expected_output else None,
        )
        verdict = llm_judge_call(
            system_prompt=_DEFAULT_SYSTEM_PROMPT, user_prompt=user_prompt,
            output_schema=output_schema, config=config)
        metadata = verdict.model_dump(exclude={"score"})
        return verdict.score, metadata

    return eval_function

"""Shared protocol data types for the evaluator system.

These ARE the contract every part of the evaluator passes around -- the orchestration, the
eval-function helpers, and the artifact agent user sim all import them from here. See
docs/evaluator_system_design.md (section 3).

JSON (de)serialization: every type carries `to_dict` / `from_dict`. Binary `File.file_data` is
base64-encoded in dicts so transcripts and reports are plain JSON; a File sourced from a dataset's
`file_path` instead stays a pointer until `materialize()` reads it (see `File`). The sim-visible
views (`ArtifactAgentOutput.sim_view`, `File.metadata`) deliberately drop `file_data`.
"""
from __future__ import annotations

import base64
import json as _json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Callable

# Allowed file types for the session API (design: /query_session). Anything else is rejected.
ALLOWED_FILE_TYPES: tuple[str, ...] = ("pdf", "docx", "pptx", "csv", "xlsx", "jpg", "png", "txt", "md")


def canonical_file_type(label: Any, file_name: Any) -> Any:
    """The `file_type` to use for a returned file. A label already in ALLOWED_FILE_TYPES is kept.
    Any other label (e.g. a MIME type such as `application/vnd.openai.pptx`) is replaced by the
    extension of `file_name` when that is allowed; otherwise it is returned unchanged so the File
    allow-list still rejects it."""
    if label in ALLOWED_FILE_TYPES or not isinstance(file_name, str):
        return label
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return ext if ext in ALLOWED_FILE_TYPES else label


# Default multi-turn stop criterion (design: Datasets).
DEFAULT_STOP_CRITERIA = "The user task has been successfully completed."

# How a generation unit's conversation ended, recorded on GenerationRecord.termination_reason so an
# eval function can tell a conversation that FINISHED from one that was merely CUT OFF. The
# benchmark only records the fact; what it means for a score is the task's call (a turn ceiling is a
# failure for a tau2-derived reward, but may be perfectly fine for an open-ended generation task).
TERMINATION_USER_STOP = "user_stop"    # the user sim judged its stop criteria met and ended the turn loop
TERMINATION_COMPLETE = "complete"      # single-turn: the one query was answered, nothing left to do
TERMINATION_MAX_TURNS = "max_turns"    # the turn ceiling was hit with the sim still talking -- TRUNCATED
TERMINATION_MAX_STEPS = "max_steps"    # the shared speaker/environment simulation budget was exhausted
TERMINATION_ERROR = "error"            # a query timed out or raised; the conversation is partial
TERMINATION_INFRASTRUCTURE_ERROR = "infrastructure_error"  # no task score for this attempt
# Terminations that mean the conversation did NOT reach its own end. Mirrors the complement of
# tau2-bench's {AGENT_STOP, USER_STOP} reward gate (src/tau2/evaluator/evaluator.py).
PREMATURE_TERMINATIONS: frozenset[str] = frozenset({
    TERMINATION_MAX_TURNS, TERMINATION_MAX_STEPS, TERMINATION_ERROR,
})


class EvalTypeError(ValueError):
    """Raised when a payload cannot be parsed into a well-formed evaluator type."""


# --- artifact I/O ------------------------------------------------------------------------------
@dataclass
class File:
    """A file passed to / produced by the artifact agent. `file_data` is raw bytes in memory.

    `file_path` is a dataset-authoring convenience (never part of the /query_session wire format):
    when a sample references a file by path instead of inlining it, `from_dict` resolves the path
    but does NOT read it -- `materialize()` does that, lazily, so `dataset.py` can load a whole
    directory of samples without pulling every file's bytes into RAM upfront."""
    file_type: str
    file_name: str
    file_data: bytes = b""
    file_path: str | None = None

    def __post_init__(self) -> None:
        if self.file_type not in ALLOWED_FILE_TYPES:
            raise EvalTypeError(
                f"file_type {self.file_type!r} not allowed; must be one of {ALLOWED_FILE_TYPES}"
            )

    def metadata(self) -> dict:
        """Sim-visible view: type + name + existence only -- never the bytes (design: G3)."""
        return {"file_type": self.file_type, "file_name": self.file_name}

    def materialize(self) -> None:
        """Read `file_path` into `file_data` if not already resident. No-op otherwise. Mutates in
        place -- once read, the bytes are cached (a File sourced from a dataset is reused across
        `repeat` units and through scoring, so re-reading on every access would waste I/O for no
        memory benefit)."""
        if not self.file_data and self.file_path:
            self.file_data = pathlib.Path(self.file_path).read_bytes()

    def to_dict(self, *, include_data: bool = True) -> dict:
        d = self.metadata()
        if include_data:
            if self.file_data:
                d["file_data"] = base64.b64encode(self.file_data).decode("ascii")
            elif self.file_path:
                # Not yet materialized -- emit the pointer rather than forcing a read here. This
                # matters for eval_system.py's config fingerprint, which calls to_dict() on every
                # sample before generation starts; forcing a read here would defeat lazy loading.
                d["file_path"] = self.file_path
        return d

    @classmethod
    def from_dict(cls, d: dict, *, base_dir: "pathlib.Path | None" = None) -> "File":
        raw = d.get("file_data")
        path = d.get("file_path")
        ftype = canonical_file_type(d["file_type"], d.get("file_name"))
        if raw is not None:
            data = base64.b64decode(raw) if isinstance(raw, str) else (raw or b"")
            return cls(file_type=ftype, file_name=d["file_name"], file_data=data)
        if path is not None:
            resolved = (base_dir / path) if base_dir is not None else pathlib.Path(path)
            if not resolved.exists():
                raise EvalTypeError(
                    f"file_path {path!r} for {d.get('file_name')!r} does not exist: {resolved}")
            return cls(file_type=ftype, file_name=d["file_name"], file_path=str(resolved))
        return cls(file_type=ftype, file_name=d["file_name"])


def _parse_files(items: Any, *, base_dir: "pathlib.Path | None" = None) -> list[File]:
    return [File.from_dict(f, base_dir=base_dir) for f in (items or [])]


def materialize_files(io: "ArtifactAgentQuery | ArtifactAgentOutput | MultiTurnInput | None") -> None:
    """Read every lazily-sourced File under `io` into memory. Called right before a query/expected
    output crosses a boundary we don't control (the artifact client, an eval function) -- laziness
    can only live between "on disk" and "about to be used", never past that boundary."""
    if io is not None:
        for f in io.files:
            f.materialize()


@dataclass
class ArtifactAgentQuery:
    """Input to /query_session (the `query` field). All fields optional; `files` may be empty."""
    text: str | None = None
    json: str | None = None
    files: list[File] = field(default_factory=list)

    def to_dict(self, *, include_file_data: bool = True) -> dict:
        return {"text": self.text, "json": self.json,
                "files": [f.to_dict(include_data=include_file_data) for f in self.files]}

    @classmethod
    def from_dict(cls, d: dict | None, *, base_dir: "pathlib.Path | None" = None) -> "ArtifactAgentQuery":
        d = d or {}
        return cls(text=d.get("text"), json=d.get("json"),
                   files=_parse_files(d.get("files"), base_dir=base_dir))


@dataclass
class ArtifactAgentOutput:
    """Output of /query_session. `text`/`json` are exposed to the sim; file bytes are NOT."""
    text: str | None = None
    json: str | None = None
    files: list[File] = field(default_factory=list)
    # Optional artifact-self-reported internal trace (LLM reasoning + tool calls/results) for
    # debugging. Persisted + shown in the report; deliberately NOT in sim_view (debug-only, can be
    # large, and the eval-time user-sim must not see the agent's internals).
    trace: list | None = None
    # Files the artifact returned that the evaluator refused (label outside ALLOWED_FILE_TYPES and
    # no usable extension), as {"file_type", "file_name"} -- metadata only. Lets a report say "a file
    # was returned but rejected" instead of "no file in the response". Never shown to the sim.
    rejected_files: list[dict] = field(default_factory=list)

    def to_dict(self, *, include_file_data: bool = True) -> dict:
        out = {"text": self.text, "json": self.json, "trace": self.trace,
               "files": [f.to_dict(include_data=include_file_data) for f in self.files]}
        if self.rejected_files:       # only when present, so transcripts without one are unchanged
            out["rejected_files"] = self.rejected_files
        return out

    @classmethod
    def from_dict(cls, d: dict | None, *, base_dir: "pathlib.Path | None" = None) -> "ArtifactAgentOutput":
        d = d or {}
        return cls(text=d.get("text"), json=d.get("json"), trace=d.get("trace"),
                   files=_parse_files(d.get("files"), base_dir=base_dir),
                   rejected_files=list(d.get("rejected_files") or []))

    def sim_view(self) -> dict:
        """What the artifact agent user sim may see: text + json + per-file (type, name) only.
        Never the file bytes -- files may be large and are metadata-only to the sim (design: G3).
        The internal `trace` is intentionally excluded (debug-only)."""
        return {"text": self.text, "json": self.json, "files": [f.metadata() for f in self.files]}


def extract_json_object(text: str | None) -> str | None:
    """Return the reply body if the WHOLE reply is a JSON object; otherwise None.

    The body comes back whitespace-stripped but otherwise byte-for-byte -- key order and inner
    spacing stay the artifact's, never a re-serialization of ours, because the caller hands it
    straight to `ArtifactAgentOutput.json` and a task's own metrics read it from there.

    Why this exists: the session API gives an output two slots, `text` and `json`, and eval functions
    that compare against a structured `expected_output` read `json`. But an artifact served over the
    completions protocol has exactly ONE channel back -- the assistant message's content string --
    so a task whose response contract is "return these fields" has no way to fill `json` directly.
    Without this step `json` is always None over that transport, and every structured objective
    scores against an empty payload (a real order_to_cash run scored its flag metrics at the
    dataset's base rate while its transcript judge, which reads `text`, scored 0.86 -- the same
    answers, read two ways).

    STRICT BY DESIGN: the whole body, or nothing. A task whose contract says the answer is a JSON
    object gets exactly that checked; a reply that narrates around its payload has not met the
    contract, and that is the task's metrics' business to report -- loudly, per data point -- not
    this function's to repair. Returns the body as-is (valid by construction, it parsed), ready for
    `ArtifactAgentOutput.json`.

    An earlier version also fished an object out of surrounding prose, taking the first balanced
    `{...}` run that parsed. That is unsafe without a schema to check candidates against: an artifact
    that prints a DB row before its verdict hands the evaluator the row, whose missing flag reads as
    "not flagged" -- a fabricated miss, and one that made the fault-type objective look inapplicable
    and drop out of aggregation entirely. (The two brace scanners still in the repo --
    `helpers/llm.py`'s `_first_json_object` and `appsci_agent_user_sim`'s `usersim_core` -- are safe
    where they are precisely because a pydantic schema rejects a wrong candidate and their loops fall
    through to the next one. There is no schema here, so the first hit would be final.) Guessing which
    object is the answer is not recoverable from; requiring the contract is.

    OBJECTS ONLY. A bare `true`, `42`, or `[1, 2]` is valid JSON but is not the "key/value dict"
    every task's response contract describes.
    """
    if not text or not text.strip():
        return None
    body = text.strip()
    try:
        parsed = _json.loads(body)
    except (TypeError, ValueError):
        return None
    return body if isinstance(parsed, dict) else None


def decode_json_dict(raw: str | None) -> dict | None:
    """A JSON-encoded string decoded as a dict, or None when it is absent, unparseable, or not a dict.

    THE one implementation of this parse. It had grown seven near-copies -- one per task eval module
    plus the orchestrator's per-sample reset seam -- each re-deciding what an absent or malformed
    payload means, which is how their answers drifted apart in the first place.

    None means "nothing usable here", and is deliberately NOT `{}`. For a metric that reads required
    fields off the oracle, an empty dict reads as "nothing was required", which scores a dataset gap as
    a perfect answer; `tasks/_TEMPLATE/eval.py` documents that trap at length and is the reason the
    primitive returns None rather than the friendlier default. Callers that genuinely want the
    empty-dict reading write `or {}` at the call site, where a reader can see the choice being made.

    Takes the RAW STRING rather than the object holding it, because the callers are not all real
    dataclasses: the orchestrator's reset seam is driven in tests by sample- and payload-SHAPED stubs
    that carry a `json` attribute and nothing else. A method-based primitive quietly required the real
    type and broke every one of them.
    """
    if not raw:
        return None
    try:
        parsed = _json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _expected(expected_output) -> dict | None:
    """A sample's structured ground truth as a dict, or None when it has none.

    Exists because eval functions are handed `expected_output` as an OPTIONAL argument, so every caller
    would otherwise repeat the same None check. Reads `json` by attribute so a payload-shaped stub
    works as well as an `ArtifactAgentOutput`.
    """
    if expected_output is None:
        return None
    return decode_json_dict(getattr(expected_output, "json", None))


@dataclass
class ConversationTurn:
    """One exchange: the query sent to the artifact and the output it returned."""
    query: ArtifactAgentQuery
    response: ArtifactAgentOutput

    def to_dict(self, *, include_file_data: bool = True) -> dict:
        return {"query": self.query.to_dict(include_file_data=include_file_data),
                "response": self.response.to_dict(include_file_data=include_file_data)}

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationTurn":
        return cls(query=ArtifactAgentQuery.from_dict(d.get("query")),
                   response=ArtifactAgentOutput.from_dict(d.get("response")))


# --- dataset (design: Datasets) ----------------------------------------------------------------
@dataclass
class UserTool:
    """One tool the SIMULATED USER holds, carrying the signature it needs in order to call it.

    THE SIGNATURE TRAVELS WITH THE CASE, and that placement is the point. A simulator handed only a
    name cannot fill a tool's arguments: its own instructions forbid inventing values, so it either
    declines to act or guesses parameter names and is rejected by the environment. Everything else
    that configures this simulator per case already lives in the sample -- the task, the persona, the
    stop criteria, which tools are granted -- so the signature belongs beside them rather than behind
    a task-level pointer that the build phase would also carry and never read.

    `parameters` is the tool's JSON-Schema object exactly as an OpenAI-style function declares it
    (`{"type": "object", "properties": {...}, "required": [...]}`). Absent or empty means the tool
    takes no arguments, which the prompt then states outright -- silence there reads as "unknown" to a
    model, and is why the one zero-argument tool was the only one banking's simulator called reliably.
    """
    name: str
    description: str = ""
    parameters: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict = {"name": self.name}
        if self.description:
            out["description"] = self.description
        if self.parameters:
            out["parameters"] = self.parameters
        return out

    @classmethod
    def from_dict(cls, d: "dict | str") -> "UserTool":
        """Accepts an object, or a bare NAME for a hand-authored dataset with no signature to give.

        A bare name is not an error -- it is the pre-signature behaviour, and the prompt still lists
        the tool -- but the sim cannot fill arguments for it, so a task whose cases need a
        customer-side action should declare the full signature.
        """
        if isinstance(d, str):
            return cls(name=d)
        return cls(name=d["name"], description=d.get("description") or "",
                   parameters=dict(d.get("parameters") or {}))


@dataclass
class MultiTurnInput:
    """The seed for a multi-turn (user-simulated) case."""
    user_task: str
    user_persona: str
    user_stop_criteria: str = DEFAULT_STOP_CRITERIA
    files: list[File] = field(default_factory=list)
    #: Tools the SIMULATED USER holds and may call during the conversation -- the customer's own
    #: capabilities, not the artifact's. Some domains put the deciding action on the customer's side
    #: (submitting their own application, say), so the artifact can only advise and the user acts; a
    #: case like that is unwinnable if the sim can only talk. Empty for every task that does not model
    #: user-side actions, which keeps those runs byte-identical (the sim's prompt omits the section
    #: entirely). Each entry carries its own signature -- see `UserTool`. The sim can execute one only
    #: when the run supplies a tool executor; what a name can actually DO stays the environment's
    #: business.
    user_tools: list[UserTool] = field(default_factory=list)

    @property
    def user_tool_names(self) -> list[str]:
        """Just the granted names -- what an authorisation check compares against."""
        return [t.name for t in self.user_tools]

    def to_dict(self) -> dict:
        return {"user_task": self.user_task, "user_persona": self.user_persona,
                "user_stop_criteria": self.user_stop_criteria,
                "files": [f.to_dict() for f in self.files],
                "user_tools": [t.to_dict() for t in self.user_tools]}

    @classmethod
    def from_dict(cls, d: dict, *, base_dir: "pathlib.Path | None" = None) -> "MultiTurnInput":
        return cls(user_task=d["user_task"], user_persona=d["user_persona"],
                   user_stop_criteria=d.get("user_stop_criteria", DEFAULT_STOP_CRITERIA),
                   files=_parse_files(d.get("files"), base_dir=base_dir),
                   user_tools=[UserTool.from_dict(t) for t in (d.get("user_tools") or [])])


@dataclass
class EvalSample:
    """One data point. Exactly one of single_turn_input / multi_turn_input is set (enforced by the
    dataset loader against the dataset `type`).

    WHERE PER-DATA-POINT PARAMS GO: the `objectives` list is TASK-LEVEL (same eval functions + weights
    for every sample) -- there is no per-sample objective/metric spec. Anything an eval function needs
    that VARIES per sample lives on the sample itself:
      - `expected_output` -- the slot for structured per-sample ground truth / params. Its `json`
        field (a JSON-encoded string) carries arbitrary data, e.g. an airline sample's required action
        set: {"required_actions": [...]}. The eval function parses it and owns the comparison
        semantics (which fields matter, tolerances, trajectory/state checks); the system imposes none.
      - `eval_rubric` -- an OPTIONAL natural-language description of what "success" looks like for this
        SAMPLE, for LLM-judge-style eval functions to read. NL text only; put STRUCTURED params in
        expected_output.json, not here.

        !! NOT a place for a METRIC's own definition !! `eval_rubric` is per-DATA-POINT (it varies
        sample to sample, e.g. "watch out: this sample's source deck uses non-obvious abbreviations"),
        never per-METRIC. A metric's scoring bands/criteria (e.g. "0.8-1.0 = excellent, no fabricated
        content...") are authored ONCE by the task designer and belong BAKED INTO the eval function's
        own prompt -- same place `make_transcript_judge`'s `prompt_template` argument or
        `make_source_grounding_judge`'s `metric_rubric` argument put them (both fixed at construction
        time, applying to every sample). Do not add a "default eval_rubric" concept to a judge
        factory to route a task-wide rubric through this field -- that conflates the two and was a
        real bug once (see appsci_bench/eval/helpers/files.py's `make_source_grounding_judge`
        docstring for the fixed version and why they must stay separate channels).
    An eval function may also branch on `data_point_id` for fully per-sample logic.
    """
    data_point_id: str
    single_turn_input: ArtifactAgentQuery | None = None   # single_turn datasets: the one query
    multi_turn_input: MultiTurnInput | None = None        # multi_turn datasets: the artifact-agent-user-sim seed
    expected_output: ArtifactAgentOutput | None = None    # per-sample structured ground truth/params (see docstring)

    def oracle(self) -> dict | None:
        """This sample's structured ground truth as a dict, or None when it carries none. For callers
        that hold the sample rather than just its `expected_output` -- the orchestrator's per-sample
        env-reset seam, which reads the starting state to seed and the golden trajectory to derive the
        required-reads allowlist."""
        return _expected(self.expected_output)
    eval_rubric: str | None = None                        # per-sample NL success rubric (for LLM-judge eval fns)

    def to_dict(self) -> dict:
        return {
            "data_point_id": self.data_point_id,
            "single_turn_input": self.single_turn_input.to_dict() if self.single_turn_input else None,
            "multi_turn_input": self.multi_turn_input.to_dict() if self.multi_turn_input else None,
            "expected_output": self.expected_output.to_dict() if self.expected_output else None,
            "eval_rubric": self.eval_rubric,
        }

    @classmethod
    def from_dict(cls, d: dict, *, base_dir: "pathlib.Path | None" = None) -> "EvalSample":
        sti = d.get("single_turn_input")
        mti = d.get("multi_turn_input")
        exp = d.get("expected_output")
        return cls(
            data_point_id=d["data_point_id"],
            single_turn_input=ArtifactAgentQuery.from_dict(sti, base_dir=base_dir) if sti is not None else None,
            multi_turn_input=MultiTurnInput.from_dict(mti, base_dir=base_dir) if mti is not None else None,
            expected_output=ArtifactAgentOutput.from_dict(exp, base_dir=base_dir) if exp is not None else None,
            eval_rubric=d.get("eval_rubric"),
        )


@dataclass
class Dataset:
    """A task's eval dataset: a turn `type`, run controls, and the list of samples."""
    type: str                     # "single_turn" | "multi_turn"
    data: list[EvalSample] = field(default_factory=list)
    max_turns: int | None = 10
    repeat: int = 1
    max_steps: int | None = None


# --- objectives (design: Metrics and Objectives) -----------------------------------------------
@dataclass
class Objective:
    """One scored metric: an eval function (by importable path), its direction, and its weight.

    `enabled` controls default selection by load_objectives; disabled declarations remain available
    for inspection or explicit opt-in. `eval_function` is the resolved callable, populated eagerly so a
    bad path fails before any artifact queries. Excluded from equality/repr (it is derived)."""
    name: str
    eval_function_path: str
    positive: bool
    weight: float
    eval_function: Callable | None = field(default=None, compare=False, repr=False)
    enabled: bool = True


# --- results (design section 3.6) --------------------------------------------------------------
@dataclass
class DataPointMetricResult:
    """The score of one (data point, objective) pair for one repeat. `score` is `None` when the
    metric doesn't apply to this data point -- excluded from aggregation, never scored 0
    (design: Task-Specific Evaluation Function)."""
    data_point_id: str
    objective: str
    repeat_index: int
    score: float | None
    metadata: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {"data_point_id": self.data_point_id, "objective": self.objective,
                "repeat_index": self.repeat_index, "score": self.score,
                "metadata": self.metadata, "error": self.error}

    @classmethod
    def from_dict(cls, d: dict) -> "DataPointMetricResult":
        return cls(data_point_id=d["data_point_id"], objective=d["objective"],
                   repeat_index=d.get("repeat_index", 0), score=d.get("score"),
                   metadata=d.get("metadata") or {}, error=d.get("error"))


@dataclass
class EvalReport:
    """Final output for one task. `results` is the flat per-(dp, objective, repeat) list; `views`
    holds the four aggregation views filled by aggregate.py; `final_score` is task-wide.
    `warnings` are non-fatal aggregation-time signals (e.g. a metric that was `None` -- not
    applicable -- for every data point in the dataset), populated by aggregate.py.
    `run_id` is unique per execution but prefixed with a stable config hash (so runs of the same
    task+dataset+objectives stay groupable). `store_root` is the on-disk transcript "database" for
    this run (conversations + dumped artifacts), so it's findable after the run."""
    task_id: str
    results: list[DataPointMetricResult] = field(default_factory=list)
    views: dict[str, Any] = field(default_factory=dict)
    final_score: float | None = None
    warnings: list[str] = field(default_factory=list)
    run_id: str | None = None
    store_root: str | None = None
    # The elicitation diagnostic. It sits BESIDE results/views/final_score and is never part of any of
    # them: it measures the build conversation, not the artifact, and averaging "did the artifact work"
    # with "did the agent ask" would answer neither question.
    #
    # An eval run always populates this, INCLUDING when the task has no rubric -- that case reports
    # `status="no_rubric"` rather than absence, so a reader can tell "not measured" from "the diagnostic
    # is broken". `None` therefore means only one thing: a report built without it, i.e. one that predates
    # the field or was assembled by hand. Do not read `is not None` as "this task had a rubric"; read
    # `status`. See docs/elicitation_score_design.md.
    elicitation: "ElicitationResult | None" = None
    # The optimization diagnostic, and beside results/views/final_score for the same reason: it
    # measures the BUILD's optimization loop -- how many times it ran, why it stopped, what it
    # deployed -- not whether the artifact works.
    #
    # `None` means the report was built without it: one that predates the field, one assembled by
    # hand, or an eval run nobody told where the build's run directory is. A run whose loop never ran
    # reports a status (`disabled` / `not_attempted`) rather than absence, so read `status`, never
    # `is not None`. See docs/optimize_agent_design.md.
    optimization: "OptimizationResult | None" = None
    # How complete this report is. `partial` is True while a run is still scoring, and STAYS True on
    # the copy a killed run leaves behind -- which is the whole reason the field exists: a report
    # written after 80 of 97 cases is a real record of those 80 and must not be readable as a task
    # score. `final_score` on a partial report is the weighted mean over the cases that finished, so
    # it is a live progress figure, NOT comparable with a full run's and never with a published
    # number. Anything rendering a score is expected to carry the flag next to it (the tracer, the
    # experiments results table, `experiments show`), the way `tokens_are_partial` travels with tokens.
    #
    # `cases_scored`/`cases_total` count UNITS (data point x repeat), matching what generation runs, so
    # a repeat: 4 dataset of 10 points totals 40. Both None on a report assembled by hand or one that
    # predates the fields; that is distinguishable from a complete run, which sets them equal.
    partial: bool = False
    cases_scored: int | None = None
    cases_total: int | None = None
    # How many COPIES of the (task-env, artifact) stack the cases were spread over. 1 is the single
    # stack every run used before parallel eval existed, and is what a report assembled by hand gets.
    # Recorded because it is a condition of the measurement, not a performance detail: at 4, a case's
    # neighbours ran in other artifact processes against other stores, so two scores are strictly
    # comparable only when this matches. Anything presenting a score should be able to reach it.
    eval_lanes: int = 1
    sample_rerun: dict | None = None

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "results": [r.to_dict() for r in self.results],
                "views": self.views, "final_score": self.final_score, "warnings": self.warnings,
                "run_id": self.run_id, "store_root": self.store_root,
                "partial": self.partial, "cases_scored": self.cases_scored,
                "cases_total": self.cases_total, "eval_lanes": self.eval_lanes,
                "sample_rerun": self.sample_rerun,
                "elicitation": self.elicitation.to_dict() if self.elicitation else None,
                "optimization": self.optimization.to_dict() if self.optimization else None}

    @classmethod
    def from_dict(cls, d: dict) -> "EvalReport":
        elicitation = d.get("elicitation")
        optimization = d.get("optimization")
        return cls(task_id=d.get("task_id", "task"),
                   results=[DataPointMetricResult.from_dict(r) for r in d.get("results") or []],
                   views=d.get("views") or {}, final_score=d.get("final_score"),
                   warnings=d.get("warnings") or [], run_id=d.get("run_id"),
                   store_root=d.get("store_root"),
                   partial=bool(d.get("partial", False)),
                   cases_scored=d.get("cases_scored"), cases_total=d.get("cases_total"),
                   eval_lanes=int(d.get("eval_lanes") or 1),
                   sample_rerun=d.get("sample_rerun"),
                   elicitation=ElicitationResult.from_dict(elicitation) if elicitation else None,
                   optimization=(OptimizationResult.from_dict(optimization)
                                 if optimization else None))


# Statuses. The distinction that matters is that UNMEASURABLE is not a zero: a run where the agent
# never contacted the stakeholder scores zero, correctly and informatively, while a run whose
# conversation log is missing has no score at all. They look identical in a bare number and mean
# opposite things.
STATUS_SCORED = "scored"
STATUS_NO_RUBRIC = "no_rubric"
STATUS_NO_EXCHANGES = "no_exchanges"
STATUS_UNMEASURABLE = "unmeasurable"
#: The no-user ablation arm: no stakeholder existed, so the question this diagnostic asks has no
#: answer. A THIRD thing, distinct from both of the above. NO_EXCHANGES is the informative zero --
#: somebody was there and the agent never asked. This is not a finding about the agent: it was handed
#: the same facts as files, so scoring it 0% elicited would be a false statement, and averaging that
#: zero into a table beside control runs would understate the arm it is there to compare against.
STATUS_NO_STAKEHOLDER = "no_stakeholder"

# Per-item verdicts. The first three partition the rubric; ERROR is the judge failing to produce a
# reading at all, counted and reported on its own rather than quietly folded into MISSED.
VERDICT_ELICITED = "elicited"
VERDICT_ASKED_NOT_ELICITED = "asked_not_elicited"
VERDICT_MISSED = "missed"
VERDICT_ERROR = "error"


@dataclass
class ItemResult:
    """One item's outcome, with the evidence and reasoning that produced it.

    The quoted evidence is what makes the per-item table auditable: "asked but not elicited" merges a
    stakeholder who genuinely could not answer with a question whose answer skirted the fact, and only
    reading the exchange tells those apart. Keeping it here means that costs no second scoring pass.
    """

    item_id: str
    verdict: str
    # The rubric item's own fact -- what "this" refers to in "did the agent ask about this?". Carried on
    # the result rather than looked up at render time, so a report and a bare elicitation.json are both
    # self-describing: an id like `check_order_first_failure_wins` tells a reader almost nothing, and a
    # consumer holding only the result file has no way to resolve one.
    fact: str = ""
    evidence: str = ""
    reasoning: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {"item_id": self.item_id, "verdict": self.verdict, "fact": self.fact,
                "evidence": self.evidence, "reasoning": self.reasoning, "error": self.error}

    @classmethod
    def from_dict(cls, d: dict) -> "ItemResult":
        return cls(item_id=d["item_id"], verdict=d["verdict"], fact=d.get("fact") or "",
                   evidence=d.get("evidence") or "", reasoning=d.get("reasoning") or "",
                   error=d.get("error"))

@dataclass
class ElicitationResult:
    """A run's whole elicitation diagnostic: the status, the three rates, and the per-item table.

    Reported next to the task score and never folded into it. A weighted average of "did the artifact
    work" and "did the agent ask" would answer neither question.

    The rates are `None` whenever nothing was measured, rather than 0.0 -- see the statuses above.
    """

    task_id: str
    status: str
    total_items: int = 0
    # How many items actually got a verdict -- and the denominator of the three rates. An item the judge
    # could not read is OUR instrument failing, not the agent's omission, so it is excluded rather than
    # counted against them. That keeps the three rates summing to one, and it is why this number has to
    # travel with them: a rate over three judged items is not the same claim as a rate over fifty.
    judged_items: int = 0
    elicited_rate: float | None = None
    asked_not_elicited_rate: float | None = None
    missed_rate: float | None = None
    error_items: int = 0
    num_exchanges: int = 0
    items: list[ItemResult] = field(default_factory=list)
    transcript_source: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "status": self.status, "total_items": self.total_items,
                "judged_items": self.judged_items,
                "elicited_rate": self.elicited_rate,
                "asked_not_elicited_rate": self.asked_not_elicited_rate,
                "missed_rate": self.missed_rate, "error_items": self.error_items,
                "num_exchanges": self.num_exchanges,
                "items": [i.to_dict() for i in self.items],
                "transcript_source": self.transcript_source, "warnings": self.warnings}

    @classmethod
    def from_dict(cls, d: dict) -> "ElicitationResult":
        return cls(task_id=d.get("task_id", ""), status=d["status"],
                   total_items=d.get("total_items", 0),
                   judged_items=d.get("judged_items", 0),
                   elicited_rate=d.get("elicited_rate"),
                   asked_not_elicited_rate=d.get("asked_not_elicited_rate"),
                   missed_rate=d.get("missed_rate"), error_items=d.get("error_items", 0),
                   num_exchanges=d.get("num_exchanges", 0),
                   items=[ItemResult.from_dict(i) for i in d.get("items") or []],
                   transcript_source=d.get("transcript_source"),
                   warnings=list(d.get("warnings") or []))


# --- the optimization diagnostic ---------------------------------------------------------------
# Statuses. `scored` and `unmeasurable` are spelled the same as the elicitation statuses above, which
# is deliberate: they mean the same two things (we measured it / we could not see it) and a reader
# should not have to learn a second vocabulary per diagnostic. The two extra ones are what this
# diagnostic can distinguish and elicitation cannot: a run where the optimization route was switched
# off (the ablation arm) versus one where it existed and the agent never used it. Those are findings
# about different things -- the experiment's design and the agent's behaviour -- and a single "0
# optimization runs" would read as the same on both.
OPTIMIZATION_STATUS_SCORED = "scored"
OPTIMIZATION_STATUS_DISABLED = "disabled"
OPTIMIZATION_STATUS_NOT_ATTEMPTED = "not_attempted"
OPTIMIZATION_STATUS_UNMEASURABLE = "unmeasurable"

# Why the loop ended. Saturation and a spent budget are SEPARATE reasons and their conjunction is a
# third: "we ran out of cycles" is not evidence that a candidate cannot be improved, and a loop that
# flattened while it still had budget is the only one of these that says the optimizer had nothing
# left to find.
STOP_REASON_SATURATED = "saturated"
STOP_REASON_BUDGET_EXHAUSTED = "budget_exhausted"
STOP_REASON_SATURATED_AND_BUDGET = "saturated_and_budget_exhausted"
STOP_REASON_NOTHING_TO_FIX = "nothing_to_fix"
#: The loop's last call left a change nobody measured, so the submitted candidate is not the one any
#: score in the run refers to.
STOP_REASON_UNVERIFIED_CHANGE = "unverified_change"
#: The run died INSIDE an optimize_agent call -- a crash or a timeout -- so the loop never reached a
#: stopping decision at all. Kept apart from `still_optimizing`, which says the agent chose to move on
#: while the loop was working: that is a finding about the agent, this one is a finding about the run,
#: and every count reported under it is a floor rather than a total.
STOP_REASON_RUN_ENDED_MID_CALL = "run_ended_mid_call"
#: The loop had not stopped for any reason of its own -- the agent simply moved on.
STOP_REASON_STILL_OPTIMIZING = "still_optimizing"

# How a roll-back went, in `optimize_agent._resolve_pending`'s own words rather than a second
# vocabulary, so the same string can be found in the tool. Only `reverted` put the previous candidate
# back in front of traffic and so wrote a checkpoint; on the other three the REJECTED code is what
# stayed running, which is a fact about the artifact and not about the bookkeeping.
ROLLBACK_REVERTED = "reverted"
ROLLBACK_RESTORE_FAILED = "restore_failed"
ROLLBACK_REDEPLOY_FAILED = "redeploy_failed"
ROLLBACK_NO_SNAPSHOT = "no_snapshot"
ROLLBACK_OUTCOMES = (ROLLBACK_REVERTED, ROLLBACK_RESTORE_FAILED, ROLLBACK_REDEPLOY_FAILED,
                     ROLLBACK_NO_SNAPSHOT)


@dataclass
class OptimizationCall:
    """One `optimize_agent` call, as the run's own trace recorded it.

    The per-call table behind the counts, so a reader can see which cycle scored what, which one made
    a change, and which change would not deploy. `unfinished` is the call whose result event never
    arrived: the run ended inside it, so every measured field here is unknown rather than zero.
    """

    ordinal: int
    iteration: int | None = None
    status: str | None = None
    succeeded: bool = False
    headline_metric: float | None = None
    saturated: bool | None = None
    budget_exhausted: bool | None = None
    cycles_used: int | None = None
    cycles_allowed: int | None = None
    changed: bool = False
    #: None when the cycle changed nothing, so no deploy was attempted -- distinct from False, which
    #: is a redeploy that was attempted and failed.
    redeployed: bool | None = None
    deploy_error: str | None = None
    #: How the PREVIOUS call's change was judged, now that this call measured it: True kept, False
    #: rolled back. None when this call had no pending change to resolve -- the first call, and any
    #: call after one that changed nothing. Not False there: "no change to judge" is a different thing
    #: from "the change was rejected".
    previous_change_kept: bool | None = None
    #: The tool's own word for how the roll-back went (`reverted`, `restore_failed`,
    #: `redeploy_failed`, `no_snapshot`), or None when nothing was rolled back. Its vocabulary rather
    #: than a second one, so the same string can be found in `optimize_agent._resolve_pending`. Only
    #: `reverted` put the previous candidate back in front of traffic; the other three leave the
    #: REJECTED code running, which is a fact about the artifact and not about the bookkeeping.
    rollback: str | None = None
    #: The checkpoint the SCORE belongs to (the candidate that was deployed when the eval ran), not
    #: the one this call's change produced.
    checkpoint: str | None = None
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    unfinished: bool = False

    def to_dict(self) -> dict:
        return {"ordinal": self.ordinal, "iteration": self.iteration, "status": self.status,
                "succeeded": self.succeeded, "headline_metric": self.headline_metric,
                "saturated": self.saturated, "budget_exhausted": self.budget_exhausted,
                "cycles_used": self.cycles_used, "cycles_allowed": self.cycles_allowed,
                "changed": self.changed, "redeployed": self.redeployed,
                "deploy_error": self.deploy_error,
                "previous_change_kept": self.previous_change_kept, "rollback": self.rollback,
                "checkpoint": self.checkpoint,
                "error": self.error, "started_at": self.started_at, "ended_at": self.ended_at,
                "unfinished": self.unfinished}

    @classmethod
    def from_dict(cls, d: dict) -> "OptimizationCall":
        return cls(ordinal=d.get("ordinal", 0), iteration=d.get("iteration"),
                   status=d.get("status"), succeeded=bool(d.get("succeeded")),
                   headline_metric=d.get("headline_metric"), saturated=d.get("saturated"),
                   budget_exhausted=d.get("budget_exhausted"), cycles_used=d.get("cycles_used"),
                   cycles_allowed=d.get("cycles_allowed"), changed=bool(d.get("changed")),
                   redeployed=d.get("redeployed"), deploy_error=d.get("deploy_error"),
                   previous_change_kept=d.get("previous_change_kept"),
                   rollback=d.get("rollback"),
                   checkpoint=d.get("checkpoint"), error=d.get("error"),
                   started_at=d.get("started_at"), ended_at=d.get("ended_at"),
                   unfinished=bool(d.get("unfinished")))


def _scores(value: object) -> list[float]:
    """A list of real scores out of whatever a JSON file holds. Never raises.

    `from_dict` reads files this module did not necessarily write -- an old diagnostic, or one edited
    by hand -- so a non-list, a string in the list or a `true` (which `isinstance(x, int)` accepts) is
    dropped rather than allowed through to become a number in a report.
    """
    if not isinstance(value, list):
        return []
    return [float(v) for v in value if isinstance(v, (int, float)) and not isinstance(v, bool)]


@dataclass
class OptimizationResult:
    """A run's whole optimization diagnostic: how long the loop ran, why it stopped, and what it left.

    Reported next to the task score and never folded into it, for the same reason the elicitation
    diagnostic is: "the optimizer ran six cycles" is not a claim about whether the artifact works.

    Counts that were not measured are `None`, never 0. A loop that produced no durable candidate and a
    run that keeps no checkpoints at all are different findings, and the checkpoint counts here are
    `None` in the second case with `checkpoint_note` saying so. See
    `appsci_bench/eval/build_optimization.py` for where each number comes from.
    """

    task_id: str
    status: str
    #: Whether the run had the optimization route at all, off `build.start`. None = the trace does not
    #: say; deliberately not defaulted to True, which is the agent's own default and would label an
    #: ablation arm as an optimization run.
    optimization_enabled: bool | None = None
    #: How many times `optimize_agent` was called. Every call measures the candidate; only a call that
    #: changes it spends a cycle, which is why this and `cycles_used` are both here and differ.
    calls: int = 0
    scored_calls: int = 0
    #: Calls that refused or failed outright -- a tampered harness, an unreachable endpoint, a run
    #: that ended mid-call. They measured nothing, so they are not evidence about the candidate.
    failed_calls: int = 0
    #: Optimization cycles spent: one per change the loop made to the candidate.
    cycles_used: int | None = None
    #: The ceiling actually in force, as the tool reported it. None means "no ceiling -- run until the
    #: scores saturate", which is a real configuration and not a missing value.
    cycles_allowed: int | None = None
    #: What the run was CONFIGURED with (`build.start`), which a call may narrow but never widen. Kept
    #: beside the effective ceiling so a narrowed run is visible as one.
    configured_cycles_allowed: int | None = None
    saturated: bool | None = None
    budget_exhausted: bool | None = None
    #: The loop flattened while it still had budget to spend -- the only state that says the optimizer
    #: had nothing further to find. False when the budget would have stopped it anyway.
    early_stopped_on_saturation: bool | None = None
    stop_reason: str | None = None
    #: Redeploys the loop itself asked for, and how they went. A failed one costs the change (the tool
    #: restores the previous source), not the run.
    deploys_attempted: int = 0
    deploys_succeeded: int = 0
    deploys_failed: int = 0
    #: Verdicts the loop reached on its own changes. A change is judged by the NEXT call, so these
    #: count verdicts rather than calls and their sum is at most `calls - 1`.
    changes_kept: int = 0
    changes_rolled_back: int = 0
    #: Roll-backs that restored the previous candidate AND got it redeployed. These are redeploys the
    #: loop made that `deploys_attempted` does not count, because a restore is not a change -- and they
    #: are the difference between the deploy tally and the checkpoint tally. See `_apply_rollbacks` in
    #: `appsci_bench/eval/build_optimization.py`.
    rollback_redeploys: int = 0
    #: Roll-backs where the restore or its redeploy did not happen, so the code the loop DECIDED
    #: AGAINST is what stayed running. Kept apart from the count above rather than folded into it: one
    #: is bookkeeping, this one changes what the submitted artifact is.
    rollbacks_failed: int = 0
    # The agent's OWN eval -- the one the loop optimises against, scoring the loop's own synthetic
    # cases under the loop's own metric names. NOT the benchmark's score and not on its scale: the two
    # measure different things about the same artifact, so the distance between them is a distance and
    # never an error. `None` throughout when no call measured anything, never 0.0.
    internal_eval_first: float | None = None
    internal_eval_best: float | None = None
    #: `ordinal` of the call that measured `internal_eval_best`; the earliest one on a tie.
    internal_eval_best_call: int | None = None
    #: The checkpoint that call scored, i.e. the candidate the best score belongs to. Only set when
    #: the loop finished below its best, since that is the only case where which candidate it was
    #: changes what a reader should conclude.
    internal_eval_best_checkpoint: str | None = None
    #: The last score the loop measured. After a roll-back that is the RESTORED candidate's score, so
    #: this is what the loop ended believing about the artifact it left deployed.
    internal_eval_final: float | None = None
    #: `final - first`. None when fewer than two calls measured: with one measurement there is no
    #: change to report, and 0.0 there would read as "measured twice and nothing moved".
    internal_eval_change: float | None = None
    #: Every score the loop measured, in call order -- the whole trajectory the four fields above
    #: summarise. The same numbers as `call_details[].headline_metric`, kept flat for readers that
    #: never load the per-call table (the CSV, the tracer chip). A refused call and a call whose
    #: result never arrived contribute NOTHING, not a zero, so this is as long as `scored_calls` and
    #: can be shorter than `calls`.
    internal_eval_all_scores: list[float] = field(default_factory=list)
    #: Whether the loop ended holding a candidate it had itself scored higher earlier -- decided on
    #: the candidate's CONTENT HASH, not on `internal_eval_final < internal_eval_best`.
    #:
    #: Those two are not the same question. The keep-or-roll-back rule only accepts an improvement, so
    #: the incumbent's score cannot fall across the changes the loop made. What falls is the
    #: measurement: the loop's own eval is not deterministic, and re-scoring the identical program
    #: returns a slightly different number. A bare `final < best` fires on that, and did, on four of
    #: the six runs it flagged in the `optimize_cycles5` sweep.
    #:
    #: `None` means the two candidates could not be compared (a score with no checkpoint reference, or
    #: no readable manifest), which is never rendered as a warning. See `_apply_below_best`.
    ended_below_best: bool | None = None
    #: Durable candidates the loop left behind, by deploy outcome. `valid` is the only one that is a
    #: usable copy of an artifact.
    checkpoints_valid: int | None = None
    checkpoints_unhealthy: int | None = None
    checkpoints_failed: int | None = None
    #: Every checkpoint in the run, optimization or not -- the denominator the three counts above sit
    #: inside.
    checkpoints_in_run: int | None = None
    #: Deploys from before the first `optimize_agent` call: the build-phase deploy that got the
    #: candidate running in the first place. Ordinary, and counted only so it is not mistaken for the
    #: next field.
    deploys_before_loop: int | None = None
    #: Deploys the agent made ITSELF while the loop was in play -- after the first call, in no call's
    #: window. These are outside the keep-or-roll-back rule entirely: it restores a snapshot the LOOP
    #: took when the LOOP made a change, so an edit the agent deploys directly has no snapshot, and the
    #: next call has nothing to compare against and nothing to restore. It records the new score as a
    #: fresh baseline. That is how a run can lose a high-water candidate the loop had already accepted,
    #: and it is the only mechanism in the sweep that did.
    deploys_outside_loop: int | None = None
    #: Why the checkpoint counts are None when they are.
    checkpoint_note: str | None = None
    call_details: list[OptimizationCall] = field(default_factory=list)
    source: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "status": self.status,
                "optimization_enabled": self.optimization_enabled,
                "calls": self.calls, "scored_calls": self.scored_calls,
                "failed_calls": self.failed_calls, "cycles_used": self.cycles_used,
                "cycles_allowed": self.cycles_allowed,
                "configured_cycles_allowed": self.configured_cycles_allowed,
                "saturated": self.saturated, "budget_exhausted": self.budget_exhausted,
                "early_stopped_on_saturation": self.early_stopped_on_saturation,
                "stop_reason": self.stop_reason,
                "deploys_attempted": self.deploys_attempted,
                "deploys_succeeded": self.deploys_succeeded,
                "deploys_failed": self.deploys_failed,
                "changes_kept": self.changes_kept,
                "changes_rolled_back": self.changes_rolled_back,
                "rollback_redeploys": self.rollback_redeploys,
                "rollbacks_failed": self.rollbacks_failed,
                "internal_eval_first": self.internal_eval_first,
                "internal_eval_best": self.internal_eval_best,
                "internal_eval_best_call": self.internal_eval_best_call,
                "internal_eval_best_checkpoint": self.internal_eval_best_checkpoint,
                "internal_eval_final": self.internal_eval_final,
                "internal_eval_change": self.internal_eval_change,
                "internal_eval_all_scores": list(self.internal_eval_all_scores),
                "ended_below_best": self.ended_below_best,
                "checkpoints_valid": self.checkpoints_valid,
                "checkpoints_unhealthy": self.checkpoints_unhealthy,
                "checkpoints_failed": self.checkpoints_failed,
                "checkpoints_in_run": self.checkpoints_in_run,
                "deploys_before_loop": self.deploys_before_loop,
                "deploys_outside_loop": self.deploys_outside_loop,
                "checkpoint_note": self.checkpoint_note,
                "call_details": [c.to_dict() for c in self.call_details],
                "source": self.source, "warnings": self.warnings}

    @classmethod
    def from_dict(cls, d: dict) -> "OptimizationResult":
        return cls(task_id=d.get("task_id", ""), status=d["status"],
                   optimization_enabled=d.get("optimization_enabled"),
                   calls=d.get("calls", 0), scored_calls=d.get("scored_calls", 0),
                   failed_calls=d.get("failed_calls", 0), cycles_used=d.get("cycles_used"),
                   cycles_allowed=d.get("cycles_allowed"),
                   configured_cycles_allowed=d.get("configured_cycles_allowed"),
                   saturated=d.get("saturated"), budget_exhausted=d.get("budget_exhausted"),
                   early_stopped_on_saturation=d.get("early_stopped_on_saturation"),
                   stop_reason=d.get("stop_reason"),
                   deploys_attempted=d.get("deploys_attempted", 0),
                   deploys_succeeded=d.get("deploys_succeeded", 0),
                   deploys_failed=d.get("deploys_failed", 0),
                   changes_kept=d.get("changes_kept", 0),
                   changes_rolled_back=d.get("changes_rolled_back", 0),
                   rollback_redeploys=d.get("rollback_redeploys", 0),
                   rollbacks_failed=d.get("rollbacks_failed", 0),
                   internal_eval_first=d.get("internal_eval_first"),
                   internal_eval_best=d.get("internal_eval_best"),
                   internal_eval_best_call=d.get("internal_eval_best_call"),
                   internal_eval_best_checkpoint=d.get("internal_eval_best_checkpoint"),
                   internal_eval_final=d.get("internal_eval_final"),
                   internal_eval_change=d.get("internal_eval_change"),
                   internal_eval_all_scores=_scores(d.get("internal_eval_all_scores")),
                   ended_below_best=d.get("ended_below_best"),
                   checkpoints_valid=d.get("checkpoints_valid"),
                   checkpoints_unhealthy=d.get("checkpoints_unhealthy"),
                   checkpoints_failed=d.get("checkpoints_failed"),
                   checkpoints_in_run=d.get("checkpoints_in_run"),
                   deploys_before_loop=d.get("deploys_before_loop"),
                   deploys_outside_loop=d.get("deploys_outside_loop"),
                   checkpoint_note=d.get("checkpoint_note"),
                   call_details=[OptimizationCall.from_dict(c)
                                 for c in d.get("call_details") or []],
                   source=d.get("source"), warnings=list(d.get("warnings") or []))


@dataclass
class GenerationRecord:
    """The result of running one (data point, repeat) unit through the artifact.

    `conversation` is the captured turns; `error` is set (and the conversation may be partial) when
    a query timed out or failed, so the unit is scored as a failure downstream rather than stalling
    the run. `infrastructure_error` instead marks an invalid attempt that generation saves separately
    and excludes from scoring. `to_dict` here inlines file bytes as base64 (portable); the on-disk transcript store
    (store.py) uses its own file-reference format instead.
    """
    data_point_id: str
    repeat_index: int
    conversation: list[ConversationTurn] = field(default_factory=list)
    error: str | None = None
    # Optional per-case env state captured by generation's `read_final_state` hook (e.g.
    # {"env_state_hash": "..."} for a DB-state score). None when no such hook ran.
    final_state: dict | None = None
    # How the conversation ended (one of the TERMINATION_* constants above), so an eval function can
    # distinguish a conversation that finished from one that was cut off by the turn ceiling. `None`
    # means "not recorded" -- an eval function must treat that as unknown, NOT as premature, so a
    # hand-built record (a unit test, an old transcript) keeps scoring the way it always did.
    termination_reason: str | None = None
    # Which copy of the task environment this unit ran against ("lane-0", "lane-1", ...), when the run
    # used more than one. None means a single-environment run, which is every run before parallel eval
    # and every run at the default width.
    #
    # Recorded because it is the first thing asked when a run goes wrong in a way that is not about the
    # artifact: if one copy's environment came up bad, ITS cases fail and the others' do not, and
    # without this that shows up as a scattered 40% failure rate with nothing tying the failures
    # together. It also makes the load split across copies auditable after the fact rather than only
    # inferable from container logs while the run is still up.
    lane: str | None = None
    simulation_steps: dict | None = None
    # Recovery is infrastructure bookkeeping, separate from the case's termination and score.
    lane_cleanup_error: str | None = None
    infrastructure_error: str | None = None
    generation_attempt: int = 1

    def to_dict(self, *, include_file_data: bool = True) -> dict:
        return {"data_point_id": self.data_point_id, "repeat_index": self.repeat_index,
                "error": self.error, "final_state": self.final_state,
                "termination_reason": self.termination_reason, "lane": self.lane,
                "simulation_steps": self.simulation_steps,
                "lane_cleanup_error": self.lane_cleanup_error,
                "infrastructure_error": self.infrastructure_error,
                "generation_attempt": self.generation_attempt,
                "conversation": [t.to_dict(include_file_data=include_file_data)
                                 for t in self.conversation]}

    @classmethod
    def from_dict(cls, d: dict) -> "GenerationRecord":
        return cls(data_point_id=d["data_point_id"], repeat_index=d["repeat_index"],
                   error=d.get("error"), final_state=d.get("final_state"),
                   termination_reason=d.get("termination_reason"), lane=d.get("lane"),
                   simulation_steps=d.get("simulation_steps"),
                   lane_cleanup_error=d.get("lane_cleanup_error"),
                   infrastructure_error=d.get("infrastructure_error"),
                   generation_attempt=d.get("generation_attempt", 1),
                   conversation=[ConversationTurn.from_dict(t) for t in d.get("conversation") or []])

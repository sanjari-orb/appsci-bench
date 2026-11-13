"""Tool: one optimization cycle -- measure the deployed candidate, then improve it.

THE TWO HALVES HAVE DIFFERENT ACTORS. This tool measures, in code: the harness contract is fully
pinned (`run.py` as the entry point, the endpoint in an env var, one JSON object on stdout), so nothing
about running it needs a model, and routing it through one lost real measurements to step limits and
stray stdout. Improving is what needs judgement, so that half is handed to `mini_swe_agent` -- with the
report already on disk and the score already taken, so the sub-agent reads failures and edits the
candidate rather than producing the number it will be judged by.

WHY THE VERIFICATION LANDS ON THE NEXT CALL. The eval measures the *deployed* container over HTTP, so an
edit is invisible to it until the artifact is rebuilt -- and only this tool can rebuild, through the
orchestrator's deploy route (the sub-agent has no Docker socket, by design). A sub-agent re-running the
eval straight after its own edit would therefore measure the candidate it just replaced. So a cycle is:

    call N      tool: run eval -> sub-agent: diagnose -> edit the artifact
                tool:      record the score, keep a snapshot of the previous source, redeploy
    call N+1    tool: run eval                          <- this measures call N's change
                tool:      keep it, or restore the snapshot and redeploy

Each call runs the eval once, spends at most one sub-agent run, and attributes every score to the
candidate that was actually deployed when the eval ran. The cost is that a change is unverified until the next call, which
is why a call that changed code says so and asks to be called again -- and why the agent is told never
to `complete` straight after one.

The sub-agent is told the score to beat, so that branch lives inside its loop: when its measurement
comes in below that, the previous change is about to be rolled back and it is asked to report rather
than edit, instead of building on a candidate that is being discarded.

The loop ends on either of two conditions, reported separately because "we ran out of cycles" is not
evidence a candidate cannot be improved: the scores stop moving (`saturated`), or the run's
optimization-cycle budget is spent (`budget_exhausted`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from .base import BaseTool, ToolContext, ToolResult, load_tool_config
from .deploy_artifact import DeployArtifactTool, _resolve_deploy_url
from .eval_generator import (
    CANDIDATE_ENDPOINT_ENV_VAR,
    REQUIRED_EVAL_FILES,
    RUN_SCRIPT_FILENAME,
    eval_harness_dir,
    harness_fingerprint,
    read_sanctioned_harness,
)
from .mini_swe_agent import MiniSweAgentTool, build_sandboxed_command
from .workspace_layout import NOTE_INBOX_DIRNAME  # noqa: F401 -- re-exported for readers and tests
from .workspace_layout import OPTIMIZE_DIRNAME as _OPTIMIZE_DIRNAME
from .workspace_layout import ensure_layout, note_inbox_dir

logger = logging.getLogger(__name__)

#: Wall-clock ceiling on one harness run. The harness drives a live conversation per case against the
#: candidate, each turn an LLM call, so minutes is normal and a hung candidate is the thing this
#: catches. Generous rather than tight: a timeout here discards a real measurement, which is worse
#: than waiting.
HARNESS_TIMEOUT_S = 1800

#: The harness's stderr, kept beside its report. Not part of the contract -- it exists so a run that
#: printed something other than the contracted JSON can be diagnosed after the fact, since stdout is
#: consumed as the report and would otherwise be the only record.
EVAL_STDERR_FILENAME = "eval_stderr.log"

#: This tool's working directory, under the WORKSPACE and deliberately NOT under the artifact
#: directory. Everything written per-cycle lives here for one reason: the artifact directory's content
#: hash is what identifies a candidate -- it drives the deploy staleness check, the checkpoint name and
#: the key a score is joined back to a candidate by. A file written into it after each deploy would give
#: unchanged code a new identity on every measurement, report the agent as having abandoned work it
#: never touched, and detach each score from its checkpoint. Keeping these out means none of that
#: machinery needs a special case: a score refers to its candidate by checkpoint id.
#:
#: The layout, the owner of each node and why the tree has the shape it does are in
#: `workspace_layout`; re-exported here because tests and readers reach for it from this module.
OPTIMIZE_DIRNAME = _OPTIMIZE_DIRNAME
HISTORY_FILENAME = "score_history.json"
#: Where the sub-agent must write the eval's aggregate report, and its note on what it changed. Fixed
#: paths rather than parsed out of its prose: its final message is free text, and reading a score out of
#: that would put a second layer of interpretation between the eval and the number every accept/reject
#: decision turns on.
REPORT_FILENAME = "eval_report.json"
CHANGE_NOTE_FILENAME = "change_note.md"
#: A change made but not yet measured, carried to the next call. On disk rather than in memory because
#: the two halves of a cycle are separate tool calls, and a build can span an agent restart.
PENDING_FILENAME = "pending_attempt.json"
#: Scratch copies of the candidate's source, one per unverified change so it can be undone. Removed once
#: the change is resolved -- checkpoints are the durable record of a candidate, and keeping these would
#: duplicate them into the run's end-of-run workspace copy.
SNAPSHOTS_DIRNAME = "snapshots"

#: Liveness-probe budget. Short on purpose -- this only answers "is anything listening"; the eval run
#: that follows is where a slow candidate gets its real time.
ENDPOINT_PROBE_TIMEOUT_S = 15

#: A score this high leaves nothing to target: no weak metric and no failing case, so asking for a
#: change would be asking for a blind edit to working code.
NOTHING_TO_FIX_SCORE = 1.0
#: Improvement over the best score so far that counts as real progress. Below this, two eval runs of the
#: same candidate differing only by judge noise would read as an improvement forever.
MIN_IMPROVEMENT = 0.01
#: Consecutive measured changes allowed to produce no real progress before the trajectory is called
#: saturated. One flat attempt is ordinary; two in a row means what is being tried is not moving this
#: candidate.
SATURATION_PATIENCE = 2
#: How many already-rejected changes a cycle brief lists. The list is there to stop the sub-agent
#: re-proposing what has already been measured and thrown away; the most recent few carry that, and
#: every entry beyond them is prompt the actual instructions have to compete with.
MAX_BRIEF_REJECTED = 5
#: Per-note ceiling in that list. The sub-agent's own note is free text of up to 4000 characters, and
#: five of those would dwarf the brief they are context for.
MAX_BRIEF_NOTE_CHARS = 300


def _optimize_dir(context: ToolContext) -> str:
    """This tool's working directory for the session."""
    return os.path.join(context.workspace_dir, OPTIMIZE_DIRNAME)


def _history_path(context: ToolContext) -> str:
    """Where the score history lives -- in the workspace, never in the artifact directory."""
    return os.path.join(_optimize_dir(context), HISTORY_FILENAME)


def _report_path(context: ToolContext) -> str:
    return os.path.join(_optimize_dir(context), REPORT_FILENAME)


def _change_note_path(context: ToolContext) -> str:
    """In the sub-agent's inbox, not the tool's state directory.

    The note is the one thing the cycle sub-agent writes back, so it is the only thing that needs a
    directory the sub-agent owns -- and owning a directory means being able to unlink and rename what
    is in it, which is not something to grant over the score history or the roll-back snapshots.
    """
    return os.path.join(note_inbox_dir(context.workspace_dir), CHANGE_NOTE_FILENAME)


def _pending_path(context: ToolContext) -> str:
    return os.path.join(_optimize_dir(context), PENDING_FILENAME)


def _cycles_used(history: list[dict[str, Any]]) -> int:
    """Optimization cycles spent so far: one per recorded change to the candidate.

    Counted from the persisted history rather than a counter in memory, so the budget survives an agent
    restart and cannot be reset by re-passing a fresh cap. A call that only measured -- nothing to fix,
    saturated, or `score_only` -- records a `baseline` and spends no cycle, which is the point: the
    budget bounds sub-agent edits and rebuilds, not measurements.
    """
    return sum(1 for r in history if r.get("stage") == "attempt")


def _cycle_budget(params: dict[str, Any], context: ToolContext) -> int | None:
    """The effective ceiling on optimization cycles, or None for "run until the scores saturate".

    Two sources, and the tighter wins. The run's configured ceiling (`optimize_cycles_allowed`, from
    `budgets.build.optimize_cycles`) is the one that bounds spend, so the agent may never widen it -- a
    budget a spender can raise is not a budget. The call parameter can only narrow it, which is a real
    thing to want: an agent that plans to hand-tune the candidate itself can ask for one cycle rather
    than the five it was allowed.
    """
    caps = [
        c for c in (getattr(context, "optimize_cycles_allowed", None),
                    params.get("iteration_cycles_allowed"))
        if isinstance(c, int) and not isinstance(c, bool) and c >= 0
    ]
    return min(caps) if caps else None


def _artifact_source_names(artifact_dir: str, snapshot_root: str | None = None) -> list[str]:
    """Entries of the artifact directory that make up the candidate itself.

    Excludes only Python's build leftovers, which are import side effects rather than authored files.
    The eval harness needs no exclusion any more: it lives under the workspace, not here (see
    `eval_generator.EVAL_DIRNAME`), so a roll-back of the candidate cannot touch the instrument that
    measures it.

    `snapshot_root` is excluded too, if given, and it is not hypothetical: the deployed layout puts the
    workspace ABOVE the artifact directory, so snapshots land outside it -- but nothing in the
    `ToolContext` contract requires that, and a caller that nested the workspace inside the artifact
    directory would otherwise have `copytree` copy the snapshot into itself until the path outgrew the
    filesystem's limit. Compared on real paths, so a symlinked workspace cannot slip past the check.
    """
    try:
        names = os.listdir(artifact_dir)
    except OSError:
        return []
    blocked = {"__pycache__", ".git"}
    root = os.path.realpath(snapshot_root) if snapshot_root else None
    kept = []
    for name in names:
        if name in blocked:
            continue
        if root is not None:
            entry = os.path.realpath(os.path.join(artifact_dir, name))
            if entry == root or root.startswith(entry + os.sep):
                continue
        kept.append(name)
    return kept


class OptimizeAgentTool(BaseTool):
    """Run one optimization cycle -- measure, then improve the candidate -- via mini_swe_agent."""

    #: What a reader of the run needs to follow the loop: the score, which candidate it belongs to,
    #: whether the loop should stop and why, and what the cycle did. `cases` is deliberately absent --
    #: it is the largest part of a report and already on disk at `history_path`, which is in this list
    #: so a reader can find it.
    trace_output_keys = (
        "status", "headline_metric", "metrics_breakdown", "num_cases", "iteration",
        "checkpoint", "checkpoint_note", "endpoint_url",
        "saturated", "cycles_used", "cycles_allowed", "budget_exhausted",
        "awaiting_verification", "cycle", "history_path",
    )

    def __init__(self):
        self._config = load_tool_config("optimize_agent")

    @property
    def name(self) -> str:
        return self._config["name"]

    @property
    def description(self) -> str:
        return self._config["description"]

    @property
    def parameters(self) -> dict[str, Any]:
        return self._config["parameters"]

    # --- entry point ----------------------------------------------------------------------------

    def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        eval_dir = eval_harness_dir(context)
        history_path = _history_path(context)

        refusal = self._check_preconditions(params, context, eval_dir)
        if refusal is not None:
            return refusal

        endpoint_url = params.get("endpoint_url") or context.artifact_endpoint
        history = self._read_history(history_path)
        budget = _cycle_budget(params, context)
        pending = self._read_pending(context)

        # Whether the sub-agent may edit at all. Decided BEFORE the measurement because it goes into the
        # brief, so it rests on the state up to this call. The cheap condition the measurement itself
        # settles -- every case already passing -- is left to the sub-agent, which will have the report
        # in hand; the budget cannot be, so it is decided here.
        #
        # A pending change counts against the budget even though it is not in the history yet. The split
        # cycle spends a cycle when the change is MADE (call N) but only records it when it is MEASURED
        # (call N+1), so counting the history alone would let a budget of 1 fund two changes -- the
        # second made before the first was ever counted.
        used_before = _cycles_used(history) + (1 if pending else 0)
        budget_spent = budget is not None and used_before >= budget
        # Caller-supplied "measure, do not touch anything" (declared in tool_configs/optimize_agent.yaml,
        # which holds the wording the model reads). The agent sets it when it changed the candidate
        # itself and wants the score moved on the record without this tool attempting a change of its
        # own -- so it is one of the three things that can clear `may_edit`, and it spends no cycle.
        score_only = bool(params.get("score_only"))
        # The loop's length is the MINIMUM of the two limits: it stops when the budget runs out or when
        # the scores saturate, whichever comes first. Neither overrides the other -- a budget of 10 does
        # not license eight more attempts once the trajectory has flattened, and saturation does not
        # extend a budget of 2. Both are reported separately on the result so a reader can tell which
        # one actually ended the run.
        may_edit = not (budget_spent or score_only or self._is_saturated(history))

        # --- the cycle: measure in code, then (maybe) one sub-agent run to improve ---------------
        outcome, error = self._run_cycle(
            context, eval_dir, endpoint_url,
            may_edit=may_edit,
            score_to_beat=(pending or {}).get("score_to_beat"),
            rejected=self._rejected_changes(history),
        )
        if error is not None:
            return error

        # --- record the score, against the candidate deployed when the eval ran ------------------
        record = self._record_score(
            history_path, history, outcome["report"], endpoint_url, context,
            outcome["deploy_status"],
            stage="attempt" if pending else "baseline",
            technique=(pending or {}).get("technique"),
        )
        score = record["headline_metric"]

        # --- resolve the previous call's unverified change ----------------------------------------
        cycle: dict[str, Any] = {
            "may_edit": may_edit,
            "changed": outcome["changed"],
            "technique": outcome["technique"] if outcome["changed"] else None,
            "change_note": outcome["note"],
            "verified_previous": None,
        }
        if pending:
            verdict = self._resolve_pending(context, pending, score)
            cycle["verified_previous"] = verdict
            # Write the verdict onto the attempt record before anything else reads it. `record` is that
            # attempt right now, and the roll-back branch below rebinds the name to the incumbent, so
            # this is the last point where the rejected attempt is still in hand. A later cycle's brief
            # is built from these marks -- see `_rejected_changes`.
            self._mark_verdict(history_path, history, record, pending, verdict)
            # A rolled-back change means the score just measured belongs to code no longer running; the
            # candidate back in front of traffic is the one that scored `score_to_beat`.
            if verdict["reverted"]:
                record = self._incumbent_record(history, pending)
                if outcome["changed"]:
                    # The sub-agent edited anyway -- its brief said not to when the measurement came in
                    # below the score to beat, but a brief cannot bind it. The roll-back has ALREADY
                    # overwritten those edits (it restores the whole source tree), and they were built
                    # on a candidate that has just been discarded, so there is nothing left to deploy.
                    #
                    # Dropping the claim matters: left as `changed`, the block below would deploy the
                    # restored incumbent while recording a pending change against the REJECTED score.
                    # Next call would measure the incumbent, find it better than that rejected score,
                    # and "accept" a change that never existed -- spending a budget cycle and writing a
                    # bogus `attempt` into the trajectory saturation is judged on.
                    logger.info(
                        "The cycle edited the candidate but the previous change was rolled back; "
                        "discarding those edits, which the roll-back has already overwritten."
                    )
                    outcome["changed"] = False
                    cycle["changed"] = False
                    cycle["technique"] = None
                    cycle["edit_discarded_by_rollback"] = True
                    self._discard_snapshot(outcome["snapshot"])
                    outcome["snapshot"] = None

        # --- start a new change, if the sub-agent made one ----------------------------------------
        if outcome["changed"]:
            deploy = DeployArtifactTool().execute({}, context)
            cycle["redeployed"] = deploy.success
            if deploy.success:
                self._write_pending(context, {
                    "snapshot": outcome["snapshot"],
                    "score_to_beat": score,
                    "technique": outcome["technique"],
                    # Carried so that, if this change is rejected next call, the brief after that can say
                    # what was actually tried and not just name the technique.
                    "note": outcome["note"],
                })
            else:
                # The change will not build. Put the working source back rather than leaving the agent
                # with a broken directory it did not break.
                cycle["deploy_error"] = deploy.error
                cycle["restored"] = self._restore_source(context, outcome["snapshot"])
                if cycle["restored"]:
                    self._discard_snapshot(outcome["snapshot"])
        elif outcome["snapshot"]:
            # Nothing changed, so the snapshot taken in case something did is dead weight.
            self._discard_snapshot(outcome["snapshot"])

        history = self._read_history(history_path)
        saturated = self._is_saturated(history)
        used = _cycles_used(history)
        exhausted = budget is not None and used >= budget
        return self._result(
            history_path, record, saturated=saturated, cycle=cycle, budget=budget, used=used,
            message=self._next_step(
                score=score, cycle=cycle, saturated=saturated, exhausted=exhausted,
                budget=budget, used=used, best=self._best(history), score_only=score_only,
            ),
        )

    # --- what to tell the agent to do next ------------------------------------------------------

    @staticmethod
    def _next_step(*, score, cycle, saturated, exhausted, budget, used, best, score_only) -> str:
        """The one thing the agent should do after this call.

        A call that changed the candidate ALWAYS asks to be called again, whatever else is true: that
        change has not been measured, and `complete` straight after it would submit a candidate nothing
        has scored. That is the weak point of verifying on the next call, so it is stated outright
        rather than left for the agent to infer.
        """
        head = f"Scored {score:.3f} (best so far {best:.3f})."
        verdict = cycle.get("verified_previous")
        if verdict:
            head += f" {verdict['message']}"

        if cycle.get("changed"):
            if cycle.get("redeployed"):
                target = cycle.get("technique") or "the failing cases"
                return (
                    f"{head} This cycle then changed the candidate (targeting '{target}') and "
                    "redeployed it. That change is NOT measured yet: call optimize_agent again to "
                    "score it -- it will be kept or rolled back on the result. Do NOT call complete "
                    "before then, or you submit a candidate nothing has scored."
                )
            restored = ("The previous working source has been restored." if cycle.get("restored")
                        else "The previous source could NOT be restored -- inspect the artifact "
                             "directory before doing anything else.")
            return (
                f"{head} A change was written but would not deploy ({cycle.get('deploy_error')}). "
                f"{restored}"
            )

        if exhausted:
            return (
                f"{head} The optimization budget is spent ({used} of {budget} cycle(s)), so no further "
                "change will be attempted even if you call again. Call complete."
            )
        if score_only:
            return (
                f"{head} No change was attempted because score_only was set. Call optimize_agent "
                "without it to have the candidate improved."
            )
        if saturated:
            return (
                f"{head} The scores have saturated -- call complete, unless you want to change the "
                "candidate's approach yourself and re-score."
            )
        if score >= NOTHING_TO_FIX_SCORE:
            return (
                f"{head} Every case passes, so there is nothing for a change to target. Call "
                "complete, or broaden the eval (via eval_generator) if you think it is too easy."
            )
        return (
            f"{head} This cycle made no change to the candidate -- see `cycle.change_note` for what it "
            "found. Call optimize_agent again to try once more, or change the approach yourself."
        )

    # --- one measurement, then at most one sub-agent run ------------------------------------------------------------------

    def _run_cycle(
        self,
        context: ToolContext,
        eval_dir: str,
        endpoint_url: str,
        *,
        may_edit: bool,
        score_to_beat: float | None,
        rejected: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any] | None, ToolResult | None]:
        """Hand one cycle to `mini_swe_agent` and read back what it produced.

        Returns `(outcome, None)` or `(None, failure)`. The outcome carries the parsed report, whether
        the candidate's source actually changed (compared, not taken on trust), the change note, and the
        snapshot taken before the sub-agent ran.
        """
        report_path = _report_path(context)
        note_path = _change_note_path(context)
        # Root-owned state, a sub-agent-writable inbox for the note, and nothing else granted -- see
        # `workspace_layout` for which node has which owner and why the tree is shaped that way. The
        # report is written by this tool now, not the sub-agent, so the sub-agent needs only to READ
        # it, which the root-owned directory already allows. Best-effort: a workspace this process
        # cannot write to surfaces on the report write below, with the context of what it was writing.
        ensure_layout(context.workspace_dir)
        # Clear the previous cycle's outputs FIRST. A harness run that produces nothing usable would
        # otherwise leave the last report in place, and this call would read it as a fresh measurement
        # -- scoring the previous candidate and attributing it to this one.
        for path in (report_path, note_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                return None, ToolResult(
                    success=False, output={"path": path},
                    error=f"Could not clear the previous cycle's {path}: {e}",
                )

        harness_before = harness_fingerprint(eval_dir)

        # MEASURE FIRST, in code. The score exists before any sub-agent runs, so no step limit, no
        # missing redirect and no stray stdout can lose it -- and a call that may not edit needs no
        # sub-agent at all.
        report, error = self._measure(context, eval_dir, endpoint_url, report_path)
        if error is not None:
            return None, error

        # Checked HERE, not only after the improvement step. `_measure` can delegate a dependency
        # repair, and that sub-agent runs against the same harness -- so measuring is an actor too, not
        # just a read. Checking only after the improvement step left the measure-only paths
        # (`score_only`, spent budget, saturation) with no check at all, and with a budget of N the call
        # that resolves the last change always has the budget spent, so EVERY budgeted run ended on the
        # unchecked path. No snapshot exists yet at this point, so there is nothing to restore: the
        # score is refused and the call spends no cycle.
        if harness_fingerprint(eval_dir) != harness_before:
            return None, self._harness_tampered(eval_dir, restored=None)

        # WHICH CONTAINER THIS SCORE BELONGS TO, asked NOW rather than when the score is written.
        # The identity of the measured candidate is only knowable here: the sub-agent below edits the
        # artifact source, so by the time the record is written the deployed fingerprint no longer
        # matches anything in the manifest and the join silently fails. Every baseline in the runs on
        # disk is recorded with no checkpoint for exactly that reason -- a real, traceable score
        # attributed to nothing, which is what makes a baseline impossible to re-score later.
        deploy_status = self._deploy_status()

        if not may_edit:
            return {
                "report": report,
                "changed": False,
                "note": "",
                "snapshot": None,
                "technique": self._technique(report),
                "deploy_status": deploy_status,
            }, None

        # Snapshot before the sub-agent touches anything, so any change it makes can be undone.
        snapshot = self._snapshot_source(context)
        before = self._source_fingerprint(context)

        result = MiniSweAgentTool().execute(
            {"task": self._cycle_brief(
                context, eval_dir, endpoint_url, report, note_path,
                score_to_beat=score_to_beat, rejected=rejected,
            )},
            context,
        )
        if not result.success:
            # The measurement survives: it was taken before this ran, and it is the score of the
            # candidate as it stands. Only the improvement attempt was lost.
            logger.info("the improvement step did not complete: %s", result.error)

        note = ""
        try:
            with open(note_path) as f:
                note = f.read().strip()[:4000]
        except (FileNotFoundError, OSError):
            note = ""

        # The sub-agent may not touch the eval either -- same rule, other actor. Checked AFTER the run
        # because a brief cannot bind it: if it edited the harness, the score it just produced was taken
        # against something eval_generator never blessed, so the measurement is discarded and the
        # candidate is put back. Refusing here rather than warning keeps "the agent never changes its own
        # scorer" true of the sub-agent as well as of the agent.
        if harness_fingerprint(eval_dir) != harness_before:
            restored = self._restore_source(context, snapshot)
            if restored:
                self._discard_snapshot(snapshot)
            return None, self._harness_tampered(eval_dir, restored=restored)

        # Whether the candidate changed is decided by COMPARING the source, not by asking. A sub-agent
        # that says it fixed something and did not would otherwise start an unverified "change" that the
        # next call measures as flat -- indistinguishable from a real change that did not help, and it
        # would spend a cycle from the budget on nothing.
        changed = bool(snapshot) and self._source_fingerprint(context) != before
        return {
            "report": report,
            "changed": changed,
            "note": note,
            "snapshot": snapshot,
            "technique": self._technique(report),
            # Captured before the edit above, not now -- see where it is taken.
            "deploy_status": deploy_status,
        }, None

    @staticmethod
    def _harness_tampered(eval_dir: str, *, restored: bool | None) -> ToolResult:
        """The refusal both tamper checks return: the score is discarded, not warned about.

        `restored` is None when the tampering was caught before any snapshot existed -- nothing of the
        candidate was touched, so there was nothing to put back and saying "restored" would be a claim
        about work that never happened.
        """
        if restored is None:
            tail = " No candidate change was in flight, so nothing needed restoring."
        elif restored:
            tail = " The candidate has been restored."
        else:
            tail = (" The candidate could NOT be restored -- inspect the artifact directory.")
        return ToolResult(
            success=False,
            output={"eval_dir": eval_dir, "restored_candidate": restored},
            error=(
                f"The eval harness at {eval_dir} was modified during this call, which is not "
                "allowed -- the agent and its sub-agents change the CANDIDATE, never the thing that "
                "measures it. The score has been discarded because it was not taken against the "
                "harness eval_generator blessed." + tail
                + " Call eval_generator to write a clean harness, then optimize_agent again."
            ),
        )

    def _cycle_brief(
        self,
        context: ToolContext,
        eval_dir: str,
        endpoint_url: str,
        report: dict[str, Any],
        note_path: str,
        *,
        score_to_beat: float | None,
        rejected: list[dict[str, Any]] | None = None,
    ) -> str:
        """The task description for one cycle: improve the candidate from a score already taken.

        The measurement is NOT part of this brief. It is performed in code before the sub-agent starts
        (see `_measure`), because the harness's contract is fully pinned and nothing about running it
        needs a model -- while routing it through one lost real measurements to step limits and stray
        stdout. What the sub-agent is for is the part that does need judgement: reading the failures and
        deciding what to change. It is handed the report rather than asked to produce it.
        """
        report_path = _report_path(context)
        headline = report.get("headline_metric")
        breakdown = report.get("metrics_breakdown") or []
        parts = [
            "Improve this agent's candidate, using a score that has ALREADY been measured for you.\n\n",
            "## Step 1 -- what the eval just said\n\n",
            f"The harness at {eval_dir} was run against the deployed candidate at {endpoint_url} and "
            f"scored it **headline_metric = {headline}**.\n\n",
        ]
        if breakdown:
            parts.append("By metric:\n")
            for row in breakdown:
                if isinstance(row, dict):
                    parts.append(f"  - {row.get('metric_name')}: {row.get('score')}\n")
            parts.append("\n")
        parts.append(
            f"The full report, including every per-case result, is at {report_path}. READ IT -- the "
            "per-case detail is what tells you why a metric is low, and it is already on disk, so you "
            "do not need to run anything to get it.\n\n"
            f"For reference, that score came from running the harness exactly like this:\n\n"
            f"    {self._harness_command(eval_dir, endpoint_url)}\n\n"
            "You can read the harness to see what it measures and how it judges. Do not run it again: "
            "the score above is the one this cycle is judged on, a second run would measure the same "
            "container (your edits are not live until whatever called you rebuilds and redeploys), and "
            "it would cost you steps you need for the fix.\n\n"
            f"What IS worth doing is talking to the candidate directly. It is serving at "
            f"{endpoint_url}, so you can POST a single case's messages to it and watch what it does -- "
            "that is how you turn a failing case in the report into a diagnosis. Probing one case is "
            "cheap; re-scoring all of them is what to avoid.\n\n"
        )

        if score_to_beat is not None:
            parts.append(
                "## Step 2 -- decide whether to go on\n\n"
                f"A change was made to this candidate last cycle and has not been measured until now. "
                f"The candidate before that change scored {score_to_beat:.3f}.\n\n"
                f"- If the headline_metric above is BELOW {score_to_beat:.3f}, that change "
                "is about to be rolled back. Do NOT edit anything: an edit now would build on a "
                f"candidate that is being discarded. Write what you read to {note_path} and "
                "finish.\n"
                f"- If it is at or above {score_to_beat:.3f}, the change is being kept -- carry on to "
                "step 3 and improve it further.\n\n"
            )

        parts.append(
                "## Step 3 -- improve the candidate\n\n"
                "If every case already passes there is nothing to target: write that to "
                f"{note_path} and finish WITHOUT editing anything.\n\n"
                "Otherwise: read the report, take the weakest metric and the cases that "
                "failed under it, and work out WHY each one failed by reading the candidate's own code "
                f"in {context.artifact_dir}. Then make the smallest change that fixes the underlying "
                "cause. You may change any of:\n"
                "  - the instructions/prompt the candidate gives its own model -- usually the "
                "highest-value change: a missing rule, ambiguous wording, or no worked example of the "
                "shape that is failing;\n"
                "  - which model it calls, if the failure looks like a capability limit rather than a "
                "missing instruction;\n"
                "  - how it is structured -- add a lookup or validation step, split one model call into "
                "two, have it check its own output before returning.\n\n"
                f"Then write to {note_path}: which metric you targeted, what you changed, and why you "
                "expect it to fix those cases. Keep it short.\n\n"
        )
        parts.append(self._rejected_section(rejected))

        parts.append(
            "## Rules\n\n"
            f"- Do NOT modify ANYTHING under {eval_dir}. Not the cases, not the expected values, not "
            "the scoring logic, and not the script either -- you change the AGENT, never the thing that "
            "measures it. A candidate that can edit its own scorer cannot be measured by it, so this is "
            "checked after you finish: if the harness changed, your score is discarded and your edits "
            "are rolled back. Installing a dependency INTO THE ENVIRONMENT is fine (that is not the "
            "harness); editing a file under that directory is not. If the harness genuinely will not "
            "run, say so in your note and finish -- regenerating it is eval_generator's job, not "
            "yours.\n"
            "- Do NOT special-case the eval's inputs: no matching on a case's exact wording, ids or "
            "expected values. Fix the general behavior the cases are probing.\n"
            "- Do NOT run docker, and do NOT try to deploy or restart the candidate. Whatever called "
            "you rebuilds and redeploys once you are done. Your edit is not live until then -- which is "
            "also why re-running the eval after editing would only measure the old container again, so "
            "do not bother.\n"
            "- Leave the candidate in a state that still starts and serves POST /respond. A change that "
            "breaks startup scores 0 next cycle and gets rolled back.\n"
            f"- Do NOT write to {report_path}. That file is the harness's own output, written before "
            "you started, and it is the score this cycle is judged on -- editing it would replace a "
            f"measurement with an assertion. Your one output is the note at {note_path}.\n"
        )
        return "".join(parts)

    @staticmethod
    def _rejected_section(rejected: list[dict[str, Any]] | None) -> str:
        """The part of the brief that says what has already been tried and thrown away.

        Empty string when there is nothing to report, so a first cycle's brief is unchanged.

        A rejected change is normally not in the code any more, but a roll-back can fail, and then it is
        both rejected and still running. Saying "these are not in the candidate" across the whole list
        would be wrong in exactly that case, and wrong about the code the sub-agent is reading -- so the
        state is stated per entry rather than once at the top.
        """
        if not rejected:
            return ""
        parts = [
            "## Already tried and rejected\n\n",
            "Earlier cycles made these changes, measured them, and discarded them for not improving the "
            "score. Do not re-propose one of them: it has been tested on this candidate and did not "
            "work. Take a different angle.\n\n",
        ]
        for item in rejected:
            score = item.get("score")
            scored = (
                f" (scored {score:.3f})"
                if isinstance(score, (int, float)) and not isinstance(score, bool)
                else ""
            )
            state = "" if item.get("reverted") else " -- NOTE: this one could not be rolled back, so it IS still in the code"
            note = (item.get("note") or "").strip().replace("\n", " ")
            detail = f": {note}" if note else ""
            parts.append(f"- {item.get('technique')}{scored}{detail}{state}\n")
        parts.append("\n")
        return "".join(parts)

    # --- measuring the candidate ----------------------------------------------------------------

    @staticmethod
    def _harness_command(eval_dir: str, endpoint_url: str) -> str:
        """The one invocation the contract allows, built from the pinned entry point and env var."""
        return (
            f"cd {shlex.quote(eval_dir)} && "
            f"{CANDIDATE_ENDPOINT_ENV_VAR}={shlex.quote(endpoint_url)} "
            f"python {shlex.quote(RUN_SCRIPT_FILENAME)}"
        )

    def _run_harness(self, eval_dir: str, endpoint_url: str) -> tuple[int, str, str]:
        """Run the harness once and return `(returncode, stdout, stderr)`. Never raises.

        Wrapped in the same sandbox mini-swe uses for its own commands, because the harness is
        LLM-authored code: run unwrapped from this process it would execute as root with the harness's
        own environment, and could read the meta-agent's source at /app -- the exact exposure
        `build_sandboxed_command` exists to close. Running it here rather than through a sub-agent does
        not widen what the harness may do.

        The single seam tests replace, so they never need a real interpreter or a live candidate.
        """
        command = build_sandboxed_command(self._harness_command(eval_dir, endpoint_url))
        # start_new_session puts the shell in its own process GROUP, so a timeout can kill the whole
        # tree. The command is layered -- `env` -> `setpriv` -> `sh -c` -> `python` -- and killing only
        # the direct child leaves the harness itself running: it makes a model call per turn with an
        # inherited key, and `_measure` retries, so the orphan would run concurrently with the retry
        # against the same candidate. `build_sandboxed_command`'s docstring names the process-group
        # kill as the reason it wraps a command string instead of reimplementing upstream's runner;
        # running the harness from here has to supply the same thing.
        proc = None
        try:
            proc = subprocess.Popen(  # noqa: S602 -- the command is built here, not caller-supplied
                command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=HARNESS_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            stdout, stderr = self._kill_group(proc)
            return 124, stdout, (f"the harness did not finish within {HARNESS_TIMEOUT_S}s"
                                 + (f"; stderr so far: {stderr[-600:]}" if stderr else ""))
        except OSError as e:  # the shell itself could not be started
            return 126, "", str(e)
        return proc.returncode, stdout or "", stderr or ""

    @staticmethod
    def _kill_group(proc) -> tuple[str, str]:
        """SIGKILL the whole process group and return whatever it had already written. Never raises."""
        if proc is None:
            return "", ""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()  # the group is gone or not ours; the direct child is all we can reach
        try:
            # The group is dead, so this drains the pipes rather than waiting on the work.
            stdout, stderr = proc.communicate(timeout=30)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return "", ""
        return stdout or "", stderr or ""

    def _measure(
        self, context: ToolContext, eval_dir: str, endpoint_url: str, report_path: str,
    ) -> tuple[dict[str, Any] | None, ToolResult | None]:
        """Score the deployed candidate against the sanctioned harness, in code.

        Deliberately NOT delegated. The harness's contract is fully pinned -- entry point, the env var
        carrying the endpoint, and one aggregate JSON object on stdout -- so nothing here needs a model
        to interpret, and routing it through one cost real measurements: the sub-agent had to
        reconstruct the stdout-to-file redirect every cycle, any stray stdout line corrupted the JSON,
        and a cycle that ran the harness and then hit its step limit before saving threw the
        measurement away. The brief used to admit that last one in as many words.

        What WAS worth delegating is the recovery: a harness that will not start because a dependency
        is missing. So a failed run gets one repair attempt through `mini_swe_agent` -- allowed to
        install into the environment, never to touch the harness, which the caller's fingerprint check
        still enforces -- and then one retry.
        """
        code, stdout, stderr = self._run_harness(eval_dir, endpoint_url)
        if not self._looks_like_report(code, stdout):
            logger.info("harness run failed (exit %s); attempting one dependency repair", code)
            # The repair brief says not to touch the candidate, and a brief cannot bind it. This path
            # runs on measure-only calls too, where the caller takes no snapshot and reports
            # `changed: False` -- so an edit made here would be live, unrecorded and impossible to roll
            # back, surfacing next call as a stale artifact with nothing explaining why. Snapshotted
            # and compared, so an edit is undone rather than trusted.
            guard = self._snapshot_source(context)
            before_repair = self._source_fingerprint(context)
            repair = MiniSweAgentTool().execute(
                {"task": self._repair_brief(eval_dir, endpoint_url, stderr or stdout)}, context,
            )
            if not repair.success:
                logger.info("harness repair attempt did not complete: %s", repair.error)
            if self._source_fingerprint(context) != before_repair:
                logger.warning(
                    "the harness repair modified the candidate, which it is not allowed to do; "
                    "restoring the candidate before measuring",
                )
                self._restore_source(context, guard)
            self._discard_snapshot(guard)
            code, stdout, stderr = self._run_harness(eval_dir, endpoint_url)

        self._write_text(os.path.join(_optimize_dir(context), EVAL_STDERR_FILENAME), stderr)
        # Written even when it is not valid JSON: this file IS the record of what the harness produced,
        # and a reader diagnosing the failure needs the bytes, not a summary of them.
        self._write_text(report_path, stdout)

        # Exit 0 is REQUIRED, not just "printed something parseable". A harness that scores the cases
        # it could bind and then exits non-zero to flag the ones it could not -- exactly the shape the
        # generator brief now asks for -- would otherwise have its partial score accepted and used to
        # accept or roll back a change, with the exit code never reaching the decision.
        if code != 0:
            return None, ToolResult(
                success=False,
                output={"eval_dir": eval_dir, "exit_code": code,
                        "stderr_path": os.path.join(_optimize_dir(context), EVAL_STDERR_FILENAME)},
                error=(
                    f"The eval harness at {eval_dir} exited {code} without producing a score"
                    + (f": {stderr.strip()[:600]}" if stderr.strip() else ".")
                    + " Nothing was changed and no cycle was spent. Fix the harness with "
                    "eval_generator, or use mini_swe_agent to install what it needs (it may not edit "
                    "the harness itself), then call optimize_agent again."
                ),
            )
        return self._read_report(report_path, eval_dir)

    @staticmethod
    def _looks_like_report(code: int, stdout: str) -> bool:
        """Did that run plausibly produce the contracted report? Cheap check, to decide on a repair.

        Only asks whether there is JSON with a headline in it -- validating the VALUE is `_read_report`'s
        job, and a harness that ran and produced a bad number must not be "repaired" by installing
        packages.
        """
        if code != 0:
            return False
        try:
            parsed = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return False
        return isinstance(parsed, dict) and "headline_metric" in parsed

    @staticmethod
    def _write_text(path: str, text: str) -> None:
        """Best-effort write. A failure here loses a diagnostic, never the run."""
        try:
            with open(path, "w") as f:
                f.write(text)
        except OSError as e:
            logger.warning("could not write %s: %s", path, e)

    def _repair_brief(self, eval_dir: str, endpoint_url: str, failure: str) -> str:
        """Narrow brief for one attempt at making the harness runnable. Not a licence to change it."""
        return (
            "This agent's own evaluation harness would not run. Your ONLY job is to make it "
            "runnable, and then stop.\n\n"
            f"It is invoked exactly like this, and that invocation is fixed:\n\n"
            f"    {self._harness_command(eval_dir, endpoint_url)}\n\n"
            f"What happened:\n\n{failure.strip()[:2000]}\n\n"
            "## Rules\n\n"
            f"- You may install missing dependencies into the environment. You may NOT edit any file "
            f"under {eval_dir} -- not the cases, not the scoring logic, not the entry point. That is "
            "checked after you finish: if the harness changed, the measurement is discarded and any "
            "edits are rolled back.\n"
            "- Do NOT touch the candidate. This is not an optimization cycle; nothing you change in "
            "the candidate would be measured.\n"
            "- Do NOT write a report or a score anywhere. The harness is run again after you finish, "
            "and its own output is the measurement.\n"
            "- If the harness cannot be made to run by installing something -- it is broken rather "
            "than missing a package -- say so and finish. Rewriting it is eval_generator's job."
        )

    # --- reading the harness's report -----------------------------------------------------------

    def _read_report(
        self, report_path: str, eval_dir: str,
    ) -> tuple[dict[str, Any] | None, ToolResult | None]:
        """Parse and validate the report the sub-agent wrote.

        Validated rather than trusted, because every decision downstream -- progress, saturation,
        keep-or-roll-back -- is arithmetic on `headline_metric`, and a string or a NaN would make each of
        those comparisons quietly false, so the loop would run forever finding no progress.
        """
        try:
            with open(report_path) as f:
                report = json.load(f)
        except FileNotFoundError:
            # This tool writes that file itself, so reaching here means the write failed rather than
            # that anyone declined to do it.
            return None, ToolResult(
                success=False, output={"report_path": report_path, "eval_dir": eval_dir},
                error=(
                    f"The harness ran but its output could not be saved to {report_path}, so there is "
                    "no score. Nothing was changed and no cycle was spent. Call optimize_agent again."
                ),
            )
        except (json.JSONDecodeError, OSError) as e:
            return None, ToolResult(
                success=False, output={"report_path": report_path, "eval_dir": eval_dir},
                error=(
                    f"The eval harness printed something that is not the contracted JSON report: {e}. "
                    f"Its exact output is at {report_path} and its stderr beside it. The harness must "
                    'print ONE object to stdout -- {"headline_metric": <0..1>, "metrics_breakdown": '
                    '[...], "cases": [...]} -- and nothing else, so a stray log line or progress '
                    "output breaks it. Nothing was changed and no cycle was spent. Call eval_generator "
                    "to fix the harness, then optimize_agent again."
                ),
            )

        if not isinstance(report, dict) or "headline_metric" not in report:
            return None, ToolResult(
                success=False, output={"report_path": report_path},
                error=f"{report_path} has no 'headline_metric' field: {str(report)[:400]}",
            )
        score = report["headline_metric"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return None, ToolResult(
                success=False, output={"report_path": report_path},
                error=f"{report_path} reported headline_metric={score!r}, which is not a number.",
            )
        score = float(score)
        if score != score or score < 0.0 or score > 1.0:  # score != score catches NaN
            return None, ToolResult(
                success=False, output={"report_path": report_path},
                error=(
                    f"{report_path} reported headline_metric={score!r}, outside the required 0..1 range."
                ),
            )
        report["headline_metric"] = score
        return report, None

    # --- resolving an unverified change ---------------------------------------------------------

    def _resolve_pending(
        self, context: ToolContext, pending: dict[str, Any], score: float,
    ) -> dict[str, Any]:
        """Keep or roll back the change the previous call made, now that it has been measured."""
        beat = pending.get("score_to_beat")
        snapshot = pending.get("snapshot")
        improved = isinstance(beat, (int, float)) and score >= beat + MIN_IMPROVEMENT
        self._clear_pending(context)
        beat_text = f"{beat:.3f}" if isinstance(beat, (int, float)) else "the previous score"

        if improved:
            self._discard_snapshot(snapshot)
            return {
                "reverted": False, "accepted": True, "score_to_beat": beat, "new_score": score,
                "message": (
                    f"The previous change improved the score {beat_text} -> {score:.3f} and is kept."
                ),
            }

        outcome = self._revert_to_incumbent(context, snapshot)
        if outcome == "reverted":
            self._discard_snapshot(snapshot)
        detail = {
            "reverted": "The previous candidate has been restored and redeployed.",
            "restore_failed": "The previous candidate could NOT be restored -- the changed code is "
                              "still what is running; inspect the artifact directory.",
            "redeploy_failed": "The previous source was restored but would not redeploy -- call "
                               "deploy_artifact and read the error.",
            "no_snapshot": "No snapshot was available, so the changed code is still what is running; "
                           "review it before you call complete.",
        }[outcome]
        return {
            "reverted": outcome == "reverted", "accepted": False, "revert": outcome,
            "score_to_beat": beat, "new_score": score,
            "message": (
                f"The previous change did not improve the score ({beat_text} -> {score:.3f}). {detail}"
            ),
        }

    def _mark_verdict(
        self,
        history_path: str,
        history: list[dict[str, Any]],
        record: dict[str, Any],
        pending: dict[str, Any],
        verdict: dict[str, Any],
    ) -> None:
        """Record on the attempt whether its change was kept or rejected, and what it was.

        Without this the history says a technique was tried and what it scored, but not what the loop
        then did about it, and the two are not the same thing: whether a change survives is decided
        against `score_to_beat` plus `MIN_IMPROVEMENT`, so a later reader re-deriving it from adjacent
        scores would have to re-implement that rule and would get it wrong the moment the rule moves.

        `reverted` is kept separate from the verdict because they can disagree: a change judged no
        improvement whose snapshot could not be restored is rejected AND still running, and a brief that
        told the sub-agent it was gone would be wrong about the code in front of it.
        """
        record["verdict"] = "kept" if verdict.get("accepted") else "rejected"
        record["reverted"] = bool(verdict.get("reverted"))
        note = pending.get("note")
        if note:
            record["change_note"] = str(note)[:MAX_BRIEF_NOTE_CHARS]
        self._write_history(history_path, history)

    @staticmethod
    def _rejected_changes(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The changes this loop has already tried and thrown away, newest first.

        Fed into the next brief so the sub-agent does not spend a cycle re-proposing something the loop
        has already measured and rejected. Only attempts carrying a technique are returned: a rejected
        record with no technique names nothing to avoid, so listing it would only tell the sub-agent that
        something unnamed failed.

        Bounded at `MAX_BRIEF_REJECTED` because this text goes into every subsequent brief; an unbounded
        list would grow the prompt with each cycle and push the actual instructions further from the end.
        """
        rejected = [
            {
                "technique": r.get("technique"),
                "note": r.get("change_note"),
                "score": r.get("headline_metric"),
                # Carried because it decides what the brief can truthfully say about the code the
                # sub-agent is looking at: a rejected change that could not be reverted is still in it.
                "reverted": bool(r.get("reverted")),
            }
            for r in reversed(history)
            if r.get("verdict") == "rejected" and r.get("technique")
        ]
        return rejected[:MAX_BRIEF_REJECTED]

    @staticmethod
    def _incumbent_record(history: list[dict[str, Any]], pending: dict[str, Any]) -> dict[str, Any]:
        """A record describing the candidate now running, after a roll-back.

        The score just measured belongs to code no longer deployed, so reporting it would tell the agent
        its candidate had regressed when it is not running that code. The most recent earlier record
        whose score is the one being restored is the honest thing to report.

        The fallback is the record BEFORE the rejected one, not the rejected one itself: `score_to_beat`
        was written from a score this history already holds, so a miss means the file was replaced or
        truncated between calls -- and in that case the previous record is at least not the candidate we
        just discarded.
        """
        beat = pending.get("score_to_beat")
        for record in reversed(history[:-1]):
            if record.get("headline_metric") == beat:
                return record
        return history[-2] if len(history) > 1 else history[-1]

    # --- pending state on disk ------------------------------------------------------------------

    def _read_pending(self, context: ToolContext) -> dict[str, Any] | None:
        """The change awaiting measurement, or None. A corrupt file reads as None."""
        try:
            with open(_pending_path(context)) as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Unreadable pending-change file (%s); treating it as none.", e)
            return None
        return data if isinstance(data, dict) and data.get("snapshot") else None

    def _write_pending(self, context: ToolContext, pending: dict[str, Any]) -> None:
        """Record a change for the next call to measure. Never raises.

        A failure here costs the roll-back, not the run: the next call then reads no pending change and
        treats its measurement as a fresh baseline. That is a worse outcome than a clean keep/roll-back
        but not a wrong one -- the score is still attributed to the candidate that produced it.
        """
        try:
            os.makedirs(_optimize_dir(context), exist_ok=True)
            with open(_pending_path(context), "w") as f:
                json.dump(pending, f, indent=2)
        except OSError as e:
            logger.warning("Could not record the pending change: %s", e)

    def _clear_pending(self, context: ToolContext) -> None:
        try:
            os.unlink(_pending_path(context))
        except (FileNotFoundError, OSError):
            pass

    # --- preconditions --------------------------------------------------------------------------

    def _check_preconditions(
        self, params: dict[str, Any], context: ToolContext, eval_dir: str,
    ) -> ToolResult | None:
        """Refuse, with an instruction, unless the candidate can actually be measured.

        Returns None when every check passes. Each failure is a different thing the agent has to do
        next, so they are reported separately rather than folded into one "not ready" message.
        """
        # The eval must exist ON DISK. The context flag alone is not enough: it stays True after a later
        # sub-agent task deletes or half-rewrites the harness.
        missing = [
            f for f in REQUIRED_EVAL_FILES if not os.path.isfile(os.path.join(eval_dir, f))
        ]
        if missing:
            if not getattr(context, "eval_ready", False):
                return ToolResult(
                    success=False, output={"eval_dir": eval_dir},
                    error=(
                        "No internal eval exists yet, so there is nothing to score against. Write "
                        "eval_handoff.md (via mini_swe_agent), call eval_generator to turn it into a "
                        "runnable eval/ harness, then call optimize_agent again."
                    ),
                    invalid_call=True,
                )
            return ToolResult(
                success=False, output={"eval_dir": eval_dir, "missing": missing},
                error=(
                    f"The eval harness at {eval_dir} is incomplete -- missing: "
                    f"{', '.join(missing)}. Call eval_generator again to rewrite it."
                ),
                invalid_call=True,
            )

        endpoint_url = params.get("endpoint_url") or context.artifact_endpoint
        if not endpoint_url:
            return ToolResult(
                success=False, output={},
                error=(
                    "No deployed candidate to score. Call deploy_artifact first (or pass endpoint_url "
                    "explicitly), then call optimize_agent again."
                ),
                invalid_call=True,
            )

        # Is what is running still what is on disk? The orchestrator is the authority -- it owns the one
        # implementation of the fingerprint -- so an unreachable status route means "cannot tell", which
        # is recorded on the score rather than treated as a pass.
        status = self._deploy_status()
        if status.get("stale"):
            return ToolResult(
                success=False,
                output={
                    "endpoint_url": endpoint_url,
                    "deployed_fingerprint": status.get("deployed_fingerprint"),
                    "current_fingerprint": status.get("current_fingerprint"),
                },
                error=(
                    "The artifact directory has changed since your last deploy, so the running "
                    "container is not the code that would be scored. Call deploy_artifact, then call "
                    "optimize_agent again. (Recording eval results does not cause this -- this tool "
                    "keeps its own files outside the artifact directory.)"
                ),
                invalid_call=True,
            )
        if status.get("configured") and not status.get("deployed"):
            return ToolResult(
                success=False, output={"endpoint_url": endpoint_url},
                error=(
                    "Nothing has been deployed through deploy_artifact yet, so there is no built "
                    "candidate to score. Call deploy_artifact, then call optimize_agent again."
                ),
                invalid_call=True,
            )

        # Only `eval_generator` may create or change the eval. A candidate that can rewrite its own
        # scorer cannot be measured by it, and this is the check that makes that a rule rather than a
        # request in a prompt: the live harness must be byte-identical to the one eval_generator last
        # blessed. Enforced because it was violated on a live run -- the agent expanded eval_data.json
        # from 32 to 46 cases through a direct mini_swe_agent call, which also silently made its own
        # score trajectory span two different measurements.
        sanctioned = read_sanctioned_harness(context.workspace_dir)
        live = harness_fingerprint(eval_dir)
        if sanctioned is None:
            return ToolResult(
                success=False, output={"eval_dir": eval_dir},
                error=(
                    "This eval harness was not produced by eval_generator, so there is nothing "
                    "trustworthy to score against. Call eval_generator (it records the harness it "
                    "wrote), then call optimize_agent again."
                ),
                invalid_call=True,
            )
        if live != sanctioned:
            return ToolResult(
                success=False,
                output={"eval_dir": eval_dir, "sanctioned_fingerprint": sanctioned,
                        "current_fingerprint": live},
                error=(
                    f"The eval harness at {eval_dir} has been changed since eval_generator wrote it, "
                    "so it is no longer the eval you were measured against and any score taken now "
                    "would not be comparable to the ones already recorded. Only eval_generator may "
                    "create or change the eval -- optimize_agent changes the AGENT, never the eval. If "
                    "the eval is wrong, too easy, or will not run, update eval_handoff.md and call "
                    "eval_generator again; that resets the optimization trajectory, which is why it is "
                    "worth getting right once rather than iterating on it."
                ),
                invalid_call=True,
            )

        live_endpoint, detail = self._probe_endpoint(endpoint_url)
        if not live_endpoint:
            return ToolResult(
                success=False, output={"endpoint_url": endpoint_url, "probe": detail},
                error=(
                    f"The candidate at {endpoint_url} is not answering ({detail}), so every eval case "
                    "would score 0 and the result would look like a quality problem rather than a dead "
                    "container. Call deploy_artifact to bring it back up, then call optimize_agent "
                    "again."
                ),
                invalid_call=True,
            )
        return None

    @staticmethod
    def _probe_endpoint(endpoint_url: str) -> tuple[bool, str]:
        """Is anything listening at the candidate's endpoint?

        Any HTTP status counts as alive -- including the 404/405 a GET against a POST-only `/respond`
        route returns. Only a transport failure means dead. This separates a container that is gone from
        a candidate that answers badly; whether it answers *well* is what the eval is for.
        """
        base = endpoint_url.rstrip("/")
        for suffix in ("/respond", "/health"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        probe_url = f"{base or endpoint_url.rstrip('/')}/health"
        try:
            with urllib.request.urlopen(probe_url, timeout=ENDPOINT_PROBE_TIMEOUT_S) as resp:
                return True, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return True, f"HTTP {e.code} (answering)"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return False, f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001 -- an odd probe failure must not be read as "dead"
            logger.warning("Endpoint probe raised unexpectedly: %s", e)
            return True, f"probe inconclusive ({type(e).__name__})"

    def _deploy_status(self) -> dict[str, Any]:
        """Ask the orchestrator what is deployed, whether it is stale, and which checkpoint it is.

        Returns `{"available": False, ...}` when there is no orchestrator (local/in-process runs) or the
        route cannot be reached. That is not a failure: such runs keep no fingerprint and write no
        checkpoints, so the honest answer is "cannot tell", recorded as such on the score.
        """
        deploy_url = _resolve_deploy_url()
        if not deploy_url:
            return {"available": False, "reason": "no orchestrator (local run)"}
        status_url = deploy_url.rstrip("/") + "/status"
        try:
            req = urllib.request.Request(status_url, method="GET")
            with urllib.request.urlopen(req, timeout=ENDPOINT_PROBE_TIMEOUT_S) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not isinstance(body, dict):
                return {"available": False, "reason": "status route returned a non-object"}
            return {"available": True, **body}
        except Exception as e:  # noqa: BLE001 -- never fail a score over a missing status
            logger.info("Deploy status unavailable (%s): %s", status_url, e)
            return {"available": False, "reason": f"{type(e).__name__}: {e}"}

    # --- the score history ----------------------------------------------------------------------

    def _record_score(
        self,
        history_path: str,
        history: list[dict[str, Any]],
        report: dict[str, Any],
        endpoint_url: str,
        context: ToolContext,
        status: dict[str, Any],
        *,
        stage: str,
        technique: str | None = None,
    ) -> dict[str, Any]:
        """Append one score to the workspace score history and return the record.

        The checkpoint reference is the point of this record: `content_hash` joins it to an entry in the
        run's `checkpoints_manifest.json`, which holds the only durable copy of the files that produced
        the score. `checkpoint` is null whenever there honestly is none -- a run that keeps no
        checkpoints, or a status route that could not be reached -- and `checkpoint_note` says which, so
        a null is never mistaken for "nobody bothered to look".
        """
        checkpoint = status.get("checkpoint") if status.get("available") else None
        if checkpoint:
            note = None
        elif not status.get("available"):
            note = f"deploy status unavailable: {status.get('reason', 'unknown')}"
        elif not status.get("deployed"):
            note = "nothing was deployed through the host deploy route"
        else:
            # Deliberately does NOT claim the run keeps no checkpoints. It said that while three sat in
            # the manifest, because the status was read after the sub-agent had edited the source and
            # the deployed fingerprint no longer matched any of them. The status is now taken at
            # measure time, so reaching this branch means a genuine mismatch -- and the note says only
            # what is actually known.
            fingerprint = str(status.get("deployed_fingerprint") or "")
            note = "the deployed fingerprint matched no checkpoint in this run's manifest"
            if fingerprint:
                note += f" (deployed {fingerprint[:12]})"

        record = {
            "iteration": len(history) + 1,
            # "baseline" is a score of the candidate as found; "attempt" is a score of a candidate a
            # previous cycle changed. Saturation counts only attempts -- see `_is_saturated`.
            "stage": stage,
            "technique": technique,
            "artifact_idx": getattr(context, "artifact_idx", 0),
            "endpoint_url": endpoint_url,
            "timestamp": time.time(),
            "checkpoint": {
                "name": checkpoint.get("name"),
                "ordinal": checkpoint.get("ordinal"),
                "content_hash": checkpoint.get("content_hash"),
                "outcome": checkpoint.get("outcome"),
            } if checkpoint else None,
            "checkpoint_note": note,
            "headline_metric": report["headline_metric"],
            "metrics_breakdown": report.get("metrics_breakdown") or [],
            "cases": report.get("cases") or [],
        }
        history.append(record)
        self._write_history(history_path, history)
        return record

    @staticmethod
    def _read_history(history_path: str) -> list[dict[str, Any]]:
        """Every score recorded so far, oldest first. A corrupt or absent file reads as empty.

        Replaced rather than repaired, and logged loudly. The alternative -- refusing to score because
        an earlier run left bad JSON -- would strand the agent with no way forward.
        """
        try:
            with open(history_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "Unreadable score history at %s (%s); starting a fresh one.", history_path, e)
            return []
        records = data.get("records") if isinstance(data, dict) else data
        if not isinstance(records, list):
            logger.warning(
                "Score history at %s has no record list; starting a fresh one.", history_path)
            return []
        return [r for r in records if isinstance(r, dict)]

    @staticmethod
    def _write_history(history_path: str, history: list[dict[str, Any]]) -> None:
        """Persist the score history. Never raises -- losing the file must not lose the score."""
        try:
            os.makedirs(os.path.dirname(history_path), exist_ok=True)
            with open(history_path, "w") as f:
                json.dump({"records": history}, f, indent=2)
        except OSError as e:
            logger.warning("Could not write the score history to %s: %s", history_path, e)

    # --- saturation -----------------------------------------------------------------------------

    @staticmethod
    def _best(history: list[dict[str, Any]]) -> float:
        """Best score recorded so far, or 0.0 when there is none."""
        scores = [
            r["headline_metric"] for r in history
            if isinstance(r.get("headline_metric"), (int, float))
            and not isinstance(r.get("headline_metric"), bool)
        ]
        return max(scores) if scores else 0.0

    @staticmethod
    def _best_before(history: list[dict[str, Any]], upto: int) -> float | None:
        """Best score among the first `upto` records, or None when there are none."""
        scores = [
            r["headline_metric"] for r in history[:upto]
            if isinstance(r.get("headline_metric"), (int, float))
            and not isinstance(r.get("headline_metric"), bool)
        ]
        return max(scores) if scores else None

    @classmethod
    def _is_saturated(cls, history: list[dict[str, Any]]) -> bool:
        """Have the last `SATURATION_PATIENCE` measured changes stopped making real progress?

        Each is measured against the best score *as of when it started*, not against the record before
        it, so a pair of changes that swings down and back up is not mistaken for progress. Changes, not
        records: a plain baseline measurement is not an attempt at anything, and counting it would call
        a candidate saturated before it had been touched.
        """
        attempts = [i for i, r in enumerate(history) if r.get("stage") == "attempt"]
        if len(attempts) < SATURATION_PATIENCE:
            return False
        for idx in attempts[-SATURATION_PATIENCE:]:
            score = history[idx].get("headline_metric")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                return False
            prior_best = cls._best_before(history, idx)
            if prior_best is None or score - prior_best >= MIN_IMPROVEMENT:
                return False
        return True

    # --- naming what a change was aimed at ------------------------------------------------------

    @staticmethod
    def _technique(report: dict[str, Any]) -> str:
        """The weakest metric's name, used to label what a change targeted."""
        breakdown = report.get("metrics_breakdown")
        if not isinstance(breakdown, list):
            return "the failing cases"
        usable = [
            m for m in breakdown
            if isinstance(m, dict) and isinstance(m.get("metric_name"), str)
            and isinstance(m.get("score"), (int, float)) and not isinstance(m.get("score"), bool)
        ]
        if not usable:
            return "the failing cases"
        return min(usable, key=lambda m: m["score"])["metric_name"]

    # --- source snapshot / revert ---------------------------------------------------------------

    @staticmethod
    def _snapshot_root(context: ToolContext) -> str:
        return os.path.join(_optimize_dir(context), SNAPSHOTS_DIRNAME)

    def _snapshot_dir(self, context: ToolContext, artifact_idx: int) -> str:
        return os.path.join(self._snapshot_root(context), f"incumbent-{artifact_idx:03d}")

    def _source_fingerprint(self, context: ToolContext) -> str:
        """A content hash of the candidate's own source, to tell whether a cycle changed it.

        Local to this tool and deliberately NOT the orchestrator's `artifact_fingerprint`: that one is
        host-side and this runs in the agent container, which does not ship `appsci_bench`. Only ever
        compared against itself within one call, so the two never need to agree.
        """
        digest = hashlib.sha256()
        root = context.artifact_dir
        for name in sorted(_artifact_source_names(root, self._snapshot_root(context))):
            path = os.path.join(root, name)
            if os.path.isfile(path):
                digest.update(name.encode("utf-8", "replace"))
                digest.update(self._file_bytes(path))
                continue
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", ".git"))
                for fname in sorted(filenames):
                    if fname.endswith(".pyc"):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    rel = os.path.relpath(fpath, root)
                    digest.update(rel.encode("utf-8", "replace"))
                    digest.update(self._file_bytes(fpath))
        return digest.hexdigest()

    @staticmethod
    def _file_bytes(path: str) -> bytes:
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return b"<unreadable>"

    def _snapshot_source(self, context: ToolContext) -> str | None:
        """Copy the candidate's source aside so a change can be undone. Never raises.

        Returns the snapshot path, or None when it could not be taken -- in which case the cycle still
        proceeds, but its outcome says plainly that a roll-back is impossible rather than promising one
        that would silently not happen.
        """
        try:
            dest = self._snapshot_dir(context, getattr(context, "artifact_idx", 0))
            if os.path.exists(dest):
                shutil.rmtree(dest)
            os.makedirs(dest, exist_ok=True)
            names = _artifact_source_names(context.artifact_dir, self._snapshot_root(context))
            if not names:
                logger.warning("Nothing to snapshot: %s has no source files.", context.artifact_dir)
                return None
            for name in names:
                src = os.path.join(context.artifact_dir, name)
                target = os.path.join(dest, name)
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.copytree(src, target, symlinks=True)
                else:
                    shutil.copy2(src, target, follow_symlinks=False)
            return dest
        except OSError as e:
            logger.warning("Could not snapshot the artifact source: %s", e)
            return None

    def _restore_source(self, context: ToolContext, snapshot: str | None) -> bool:
        """Put the snapshotted source back, dropping whatever the cycle wrote. Never raises.

        The eval harness is untouched throughout -- it is not part of the snapshot -- so the eval later
        scores are compared against survives a roll-back, and so does the score history.
        """
        if not snapshot or not os.path.isdir(snapshot):
            return False
        try:
            for name in _artifact_source_names(context.artifact_dir, self._snapshot_root(context)):
                path = os.path.join(context.artifact_dir, name)
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
            for name in os.listdir(snapshot):
                src = os.path.join(snapshot, name)
                target = os.path.join(context.artifact_dir, name)
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.copytree(src, target, symlinks=True)
                else:
                    shutil.copy2(src, target, follow_symlinks=False)
            return True
        except OSError as e:
            logger.error("Could not restore the artifact source from %s: %s", snapshot, e)
            return False

    def _revert_to_incumbent(self, context: ToolContext, snapshot: str | None) -> str:
        """Restore AND redeploy the incumbent. Returns which of four things happened.

        Restoring the files is not enough on its own: the container still running is the rejected
        candidate. The pre-eval staleness check catches an edit-without-redeploy but not a
        deploy-of-a-rejected-candidate, so leaving it up would submit the wrong artifact.
        """
        if not snapshot:
            return "no_snapshot"
        if not self._restore_source(context, snapshot):
            return "restore_failed"
        if not DeployArtifactTool().execute({}, context).success:
            return "redeploy_failed"
        return "reverted"

    @staticmethod
    def _discard_snapshot(snapshot: str | None) -> None:
        """Delete a snapshot whose change is resolved. Never raises.

        Cleaned up rather than kept because `/workspace` is copied out at the end of a run: a snapshot
        per cycle would duplicate into that copy the candidates the run's checkpoints already hold
        durably, which is what checkpoints are for. A snapshot whose restore FAILED is deliberately left
        in place by the caller -- it is then the only copy of the working candidate.
        """
        if not snapshot:
            return
        shutil.rmtree(snapshot, ignore_errors=True)

    # --- result shaping -------------------------------------------------------------------------

    @staticmethod
    def _result(
        history_path: str,
        record: dict[str, Any],
        *,
        saturated: bool,
        cycle: dict[str, Any],
        message: str,
        budget: int | None,
        used: int,
    ) -> ToolResult:
        """One result shape for every successful call, whatever the call decided to do.

        `saturated` and `budget_exhausted` are reported separately and never collapsed. They are
        different claims -- the scores stopped moving, versus the allowance ran out -- and a reader who
        saw "saturated" when the truth was "we ran out of budget at cycle 2" would conclude the
        candidate cannot be improved, which is exactly what the run did not establish.

        `cases` is deliberately NOT returned: the per-case detail is the largest part of a report and is
        already on disk at `history_path`, so echoing it into the conversation would spend the agent's
        context on data it can read if it wants it.
        """
        exhausted = budget is not None and used >= budget
        awaiting = bool(cycle.get("changed") and cycle.get("redeployed"))
        if awaiting:
            status = "awaiting_verification"
        elif exhausted:
            status = "budget_exhausted"
        elif saturated:
            status = "saturated"
        elif record["headline_metric"] >= NOTHING_TO_FIX_SCORE:
            # Its own status, because "optimizing" contradicted the message. A call that scores a
            # perfect candidate tells the agent to call complete, and reporting that as `optimizing`
            # made the machine-readable field say keep going while the prose said stop.
            status = "nothing_to_fix"
        else:
            status = "optimizing"
        return ToolResult(
            success=True,
            output={
                "status": status,
                "headline_metric": record["headline_metric"],
                "metrics_breakdown": record.get("metrics_breakdown") or [],
                "num_cases": len(record.get("cases") or []),
                "iteration": record.get("iteration"),
                "checkpoint": record.get("checkpoint"),
                "checkpoint_note": record.get("checkpoint_note"),
                "endpoint_url": record.get("endpoint_url"),
                "saturated": saturated,
                "cycles_used": used,
                # None means no ceiling: keep going until the scores saturate.
                "cycles_allowed": budget,
                "budget_exhausted": exhausted,
                # True when this call left a change that has NOT been measured yet. The agent must call
                # again before `complete`, or it submits a candidate nothing has scored.
                "awaiting_verification": awaiting,
                "cycle": cycle,
                "history_path": history_path,
                "message": message,
            },
        )

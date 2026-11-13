"""Tool: generate a runnable internal eval harness for the candidate artifact.

Thin wrapper, same shape as deploy_artifact.py: check a precondition, delegate to
mini_swe_agent, verify the delegate actually produced the promised files, then set a context
flag. The precondition is a handoff file (`eval_handoff.md`) the agent must have already written
-- this tool does not invent an understanding of the task itself, it only turns an existing one
into a runnable eval.

TODO: this is expected to run once per BUILD session. `optimize_agent` scores repeatedly against the
one eval/ harness it produces AND decides when to stop by comparing those scores to each other, so a
dataset swap partway through would silently make that comparison meaningless -- the trajectory would
span two different measurements, and a "saturated" or "improved" verdict would be about the eval rather
than the candidate. If the agent should later be allowed to regenerate the eval mid-session (e.g. it
decides to tackle a new behavior and wants a refreshed/expanded dataset), decide first how the score
history handles the discontinuity: either scope saturation to a single dataset generation, or keep
scoring the older datasets too.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolContext, ToolResult, load_tool_config
from .mini_swe_agent import MiniSweAgentTool
from .workspace_layout import HANDOFF_DIRNAME, ensure_layout, handoff_dir

logger = logging.getLogger(__name__)

HANDOFF_FILENAME = "eval_handoff.md"
#: The harness AND the handoff that specifies it live under the WORKSPACE, not the artifact directory,
#: for two reasons that have nothing to do with each other:
#:
#: 1. It is not part of the deliverable. The artifact directory IS the Docker build context, so a harness
#:    kept there ships the task's test data and an LLM-calling judge script into the image a customer
#:    would deploy.
#: 2. The artifact directory's content hash identifies the candidate -- it drives the deploy staleness
#:    check and the checkpoint name. A harness written or edited there after a deploy makes the artifact
#:    read as stale, so `optimize_agent` refuses to score until the agent redeploys. Observed on a live
#:    order_to_cash run: three rebuilds and two wasted turns for changes that never touched the
#:    candidate's own code.
#:
#: The handoff was left in the artifact directory when the harness moved out, and it has both problems.
#: Reason 2 is the visible one -- writing it dirties the fingerprint, so the very next `optimize_agent`
#: call reports the artifact as stale and costs a redeploy for a file that cannot change the candidate's
#: behaviour. Reason 1 has a sharper edge here than it does for the harness: everything in the artifact
#: directory is snapshotted before a cycle and restored when that cycle is rejected, while this tool and
#: `optimize_agent` both tell the agent to update the handoff when the eval is wrong. A rejected cycle
#: therefore reverted the correction, and the next generation read the stale description back.
#:
#: `/workspace` is copied out of the container at the end of a run, so both still outlive it.
EVAL_DIRNAME = "eval"
#: The harness's entry point. One definition, because two halves of the contract read it: this tool
#: checks the file exists after generating, and `optimize_agent` builds the invocation that runs it.
#: Held here, with the rest of the harness contract, because this tool is what produces the harness --
#: and imported there rather than restated, so a rename cannot leave the existence check and the
#: invocation disagreeing about which file the entry point is.
RUN_SCRIPT_FILENAME = "run.py"
REQUIRED_EVAL_FILES = ("eval_data.json", RUN_SCRIPT_FILENAME, "README.md")
# The one fixed way `run.py` must read the candidate's endpoint -- pinned so `optimize_agent`
# can invoke a freshly-authored `run.py` the same way every time, with no LLM in the loop to read
# its README and figure out an ad hoc convention. Shared with optimize_agent.py.
CANDIDATE_ENDPOINT_ENV_VAR = "CANDIDATE_ENDPOINT_URL"

#: Record of the harness THIS tool produced, written in the workspace -- never in the artifact
#: directory, whose content hash identifies the candidate. `optimize_agent` compares the live harness
#: against it and refuses to score when they differ.
#:
#: This is what makes "only eval_generator may change the eval" a rule rather than a request. The agent
#: being scored can otherwise rewrite its own scorer, and it did: on a live order_to_cash run it
#: expanded eval_data.json from 32 to 46 cases through a direct mini_swe_agent call -- because this
#: tool's own success message used to invite exactly that. Making the eval HARDER is the benign version;
#: nothing distinguished it from making it easier, and either way a score trajectory spanning the change
#: compares two different measurements.
SANCTION_FILENAME = ".eval_generation.json"


def harness_fingerprint(eval_dir: str) -> str:
    """Content hash of the eval harness -- of what the candidate is measured BY.

    Skips Python's build leftovers, so merely RUNNING the harness (which writes caches) does not read as
    editing it. Returns "" when there is no harness, which callers treat as "nothing to compare".
    """
    root = Path(eval_dir)
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        digest.update(str(rel).encode("utf-8", "replace"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def eval_harness_dir(context: ToolContext) -> str:
    """Where the eval harness lives: under the workspace, never in the artifact directory."""
    return os.path.join(context.workspace_dir, EVAL_DIRNAME)


def handoff_path(context: ToolContext) -> str:
    """Where the eval handoff lives: its own directory under the workspace.

    Not the artifact directory, for the two reasons above. Not the workspace root either -- these are
    this layer's bookkeeping rather than part of the task, and loose in a directory that already means
    several things to a reader they look like the agent's own output. `workspace_layout` owns the tree
    and gives this one node an owner the sub-agent can actually write as.
    """
    return os.path.join(handoff_dir(context.workspace_dir), HANDOFF_FILENAME)


def sanction_path(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, SANCTION_FILENAME)


def record_sanctioned_harness(workspace_dir: str, eval_dir: str) -> str:
    """Bless the harness now on disk as the one to measure against. Never raises.

    Returns the fingerprint recorded, or "" if it could not be written -- in which case
    `optimize_agent` finds no sanction and says so rather than trusting an unblessed harness.
    """
    fingerprint = harness_fingerprint(eval_dir)
    try:
        os.makedirs(workspace_dir, exist_ok=True)
        with open(sanction_path(workspace_dir), "w") as f:
            json.dump({"eval_dir": eval_dir, "fingerprint": fingerprint,
                       "generated_at": time.time()}, f, indent=2)
    except OSError as e:
        logger.warning("Could not record the sanctioned eval harness: %s", e)
        return ""
    return fingerprint


def read_sanctioned_harness(workspace_dir: str) -> str | None:
    """Fingerprint of the harness this tool last produced, or None when there is no record."""
    try:
        with open(sanction_path(workspace_dir)) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    fingerprint = data.get("fingerprint") if isinstance(data, dict) else None
    return fingerprint if isinstance(fingerprint, str) and fingerprint else None



class EvalGeneratorTool(BaseTool):
    """Turn an existing eval_handoff.md into a runnable eval/ harness via mini_swe_agent."""

    #: Where the harness landed and which files it holds -- enough for a reader to open the eval a
    #: later `optimize_agent` score was taken against.
    trace_output_keys = ("status", "eval_dir", "files", "missing")

    def __init__(self):
        self._config = load_tool_config("eval_generator")

    @property
    def name(self) -> str:
        return self._config["name"]

    @property
    def description(self) -> str:
        """The wording the model reads, with the handoff path rendered in.

        Interpolated rather than written into the YAML, because this description is in front of the
        model every turn: left stating a stale location, the agent writes the handoff where the
        description says and this tool refuses with a different path -- which is the failure the
        handoff move set out to fix, reintroduced one surface over. `{handoff_path}` is the only
        placeholder, so a literal brace elsewhere in the text would be a bug worth failing on.
        """
        return self._config["description"].replace(
            "{handoff_path}", os.path.join(HANDOFF_DIRNAME, HANDOFF_FILENAME))

    @property
    def parameters(self) -> dict[str, Any]:
        return self._config["parameters"]

    def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        # Defensive and idempotent: the agent builds this tree at start-up, before the sub-agent call
        # that writes the handoff. Called again here so this tool never depends on an ordering nobody
        # enforces.
        ensure_layout(context.workspace_dir)
        handoff = handoff_path(context)
        if not os.path.isfile(handoff):
            return ToolResult(
                success=False,
                output={"handoff_path": handoff},
                error=(
                    f"{handoff} does not exist yet. Ask mini_swe_agent to write it first -- "
                    "a plain-language description of the task, the candidate's interface, and how "
                    "to judge a response -- then call eval_generator again."
                ),
                # Rejected before mini_swe_agent was ever delegated to, so nothing ran. Same contract
                # as talk_to_user's missing-question rejection: the loop counts these toward its
                # stuck-run breaker, and work that ran and failed toward neither.
                invalid_call=True,
            )

        eval_dir = eval_harness_dir(context)
        extra_context = (params.get("context") or "").strip()

        task = (
            f"Read the handoff file at {handoff}, which describes the task, the candidate's "
            "interface, and what counts as a correct response. Using it, build an evaluation "
            "system -- the way an applied scientist would -- that measures how well the candidate "
            "performs the task:\n\n"
            f"1. {eval_dir}/eval_data.json -- a JSON list of test cases.\n"
            "   - If the handoff describes a single request/response interface, each case is an "
            "object with at least {id, scenario, ground_truth, metrics}; decide the exact shape of "
            "ground_truth and metrics per case based on what the handoff describes -- they don't "
            "need to be uniform across cases.\n"
            "   - If the handoff describes a multi-turn conversation instead, each case is an "
            "object with at least {id, persona, task, stop_criteria, ground_truth, metrics}: "
            "persona/task/stop_criteria describe the simulated user (who they are, what they want, "
            "when the conversation ends), and ground_truth/metrics describe how to judge the "
            "outcome. Compute ground_truth for each case by checking the actual environment/rules "
            "for that scenario (e.g. querying the database, applying the documented business "
            "rules) -- never invent a plausible-looking answer. Bake the actual looked-up data "
            "(names, amounts, numbers, prices, or whatever specifics the case involves) into "
            "ground_truth itself, not just a qualitative description of what correct behavior "
            "looks like -- a rubric with no concrete value to check against can't tell a correct "
            "specific answer from a fabricated one, and will wrongly penalize the candidate for "
            "being specific.\n"
            "   - The environment's store is NOT reset between cases, and may also carry over "
            "across repeated calls to this same eval within one BUILD session (optimize_agent "
            "can rerun eval_data.json multiple times as the candidate is iterated on). So "
            "construct cases against non-colliding underlying records -- different entities "
            "(whatever the task's domain uses as its unit of state: an account, a record, an "
            "order, etc.) per case, not the same one reused across cases -- querying the "
            "environment yourself for additional records rather than only reusing whatever the "
            "earlier exploration happened to look up. Before finalizing eval_data.json, check: "
            "does any two cases reference the same mutable entity, or does one case's write touch "
            "an entity another case reads? If so, give one of them a different entity. Only chain "
            "cases onto the same entity deliberately, when the scenario is specifically testing a "
            "multi-step sequence -- and say so in README.md.\n"
            "   - EVERY identifier in a case must be one you obtained from THIS environment "
            "through its tools. Do NOT use an id you recognise from a public benchmark or "
            "dataset, and do not guess at an id format: the store you are given is not the one a "
            "public version of this task ships, so a remembered id resolves to nothing. Work "
            "outward from what the environment actually enumerates -- a listing tool, or a search "
            "tool you can sweep over the values a listing returned -- and then follow the "
            "references the records you find carry to reach the rest. Observed on a live run: a "
            "harness seeded its discovery with 28 user ids recalled from a public dataset, every "
            "one of them 404'd against the store actually present, and the run concluded the "
            "environment was empty when it was not.\n"
            "   - Cases that could not be bound to a real record are NOT results. If a case has no "
            "record to run against, or none can be found at all, exit non-zero with the reason on "
            "stderr -- do not emit those cases as skipped and do not report a headline metric "
            "computed over cases that never ran. A harness reporting 0.0 because it found nothing "
            "is indistinguishable from a candidate that answered everything wrong, so the "
            "optimization loop reads it as a real measurement and keeps whatever change it last "
            "made. Failing outright is what gets the harness fixed instead.\n"
            f"2. {eval_dir}/{RUN_SCRIPT_FILENAME}:\n"
            "   - Build the candidate's endpoint URL defensively: strip trailing slashes from "
            f"{CANDIDATE_ENDPOINT_ENV_VAR}, and only append '/respond' if it isn't already the "
            "suffix -- do not unconditionally concatenate, or a base URL that already ends in "
            "/respond will double up and 404 on every case.\n"
            "   - Single-turn: for each case, POST to the candidate's /respond endpoint (base URL "
            f"read from the environment variable {CANDIDATE_ENDPOINT_ENV_VAR} -- do not hardcode "
            'it or invent a different flag/env var name) with body {"messages": [{"role": "user", '
            '"content": <scenario>}]}, read choices[0].message.content from the JSON response, and '
            "check it against that case's ground_truth using its metrics.\n"
            "   - Multi-turn: for each case, drive an actual conversation with the candidate at "
            "that same endpoint: maintain the message history yourself, and for each simulated-user "
            "turn call an LLM directly (make the API call yourself, with a key read from the "
            "environment -- do not import anything from appsci_bench) prompted with that case's "
            "persona/task, so it answers only from its own persona's known facts and never leads or "
            "ends the conversation itself. Stop a case on its stop_criteria or a hard cap of 8 "
            "turns, whichever comes first, then judge the outcome against ground_truth using "
            "metrics.\n"
            "   - The handoff's stated interaction pattern is binding: if it says multi-turn, use "
            "the multi-turn case shape for at least the cases whose correctness depends on how the "
            "conversation unfolds over several turns. A single pre-loaded message history that "
            "already sits at the decision point does not count as testing multi-turn behavior, "
            "even though the message list itself has multiple entries -- it's still one static "
            "snapshot, not a live back-and-forth.\n"
            "   - Whenever a case's scoring depends on checking environment/store state after the "
            "interaction (single-turn or multi-turn alike): also snapshot that same state BEFORE "
            "the interaction starts, and check whether the pre-interaction state already "
            "satisfies what the case expects to see AFTER. If so, record that on the case's "
            'result (e.g. "precondition_already_met": true) rather than scoring it as a clean '
            "pass -- given no reset between cases or between repeated eval runs, this is the "
            "concrete sign that a prior case or a prior run already produced the change this "
            "case is meant to test, not the candidate you're evaluating right now. Also tell the "
            "judge that any specific fact in ground_truth can itself be stale for the same reason "
            "(a shared entity's value may have changed since ground_truth was computed) -- when a "
            "live-checked fact conflicts with ground_truth, trust the live one.\n"
            "   - Either way: wrap each case in its own try/except so one broken or stuck case "
            "scores 0 for that case instead of crashing the whole run, then print ONE aggregate "
            "JSON object to stdout, in EXACTLY this shape:\n\n"
            "    {\n"
            '      "headline_metric": <float 0..1>,\n'
            '      "metrics_breakdown": [{"metric_name": <str>, "score": <float 0..1>}, ...],\n'
            '      "cases": [...]\n'
            "    }\n\n"
            "headline_metric is the single overall score. metrics_breakdown aggregates that score "
            "by each distinct metric name used across cases -- every metric name that appears in "
            "any case's metrics must appear here exactly once, so a reader can see which specific "
            "metric is dragging the score down. cases holds a per-case result for each entry; its "
            "exact fields are not fixed (same as ground_truth/metrics above), since different "
            "tasks need different per-case detail -- document what you chose in README.md per "
            "requirement 3 below.\n"
            f"3. {eval_dir}/README.md -- explain what the eval covers, whether cases are single- or "
            f"multi-turn, how to run {RUN_SCRIPT_FILENAME}, and how to read the headline score off "
            "its output. For each case's metrics, note which part of the "
            "handoff's judging criteria it's derived from, so the link between the handoff and the "
            "scoring is traceable, not just asserted."
        )
        if extra_context:
            task += f"\n\nAdditional guidance: {extra_context}"

        result = MiniSweAgentTool().execute({"task": task}, context)
        if not result.success:
            return ToolResult(
                success=False,
                output={"eval_dir": eval_dir, "mini_swe_agent_output": result.output},
                error=f"mini_swe_agent failed to write the eval harness: {result.error}",
            )

        missing = [
            fname for fname in REQUIRED_EVAL_FILES
            if not os.path.isfile(os.path.join(eval_dir, fname))
        ]
        if missing:
            return ToolResult(
                success=False,
                output={"eval_dir": eval_dir, "missing": missing},
                error=(
                    f"mini_swe_agent reported success but {eval_dir} is missing: "
                    f"{', '.join(missing)}. Call eval_generator again, or ask mini_swe_agent "
                    "directly to finish writing them."
                ),
            )

        context.eval_ready = True
        # Bless what was just written. Nothing else may change it: `optimize_agent` compares the live
        # harness against this and refuses to score one edited any other way.
        fingerprint = record_sanctioned_harness(context.workspace_dir, eval_dir)
        return ToolResult(
            success=True,
            output={
                "status": "eval_ready",
                "eval_dir": eval_dir,
                "files": [os.path.join(eval_dir, f) for f in REQUIRED_EVAL_FILES],
                "eval_fingerprint": fingerprint,
                "message": (
                    f"Eval harness written to {eval_dir}. This is now the ONLY sanctioned eval: do NOT "
                    "edit anything under it yourself, with mini_swe_agent or otherwise -- "
                    "optimize_agent refuses to score against a harness changed any other way, because "
                    "a candidate that can rewrite its own scorer cannot be measured by it. If the eval "
                    "is wrong, too easy, or will not run, update eval_handoff.md and call "
                    "eval_generator again so the change is grounded in your written understanding of "
                    "the task. Prefer to get it right once: scores taken against different generations "
                    "of the eval are not comparable, so regenerating resets the optimization "
                    "trajectory."
                ),
            },
        )

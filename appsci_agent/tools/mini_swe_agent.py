"""Tool for delegating coding and shell tasks to mini-swe-agent.

mini-swe-agent is a specialized subagent that handles implementation tasks
autonomously using bash, file operations, and code editing.
"""
from __future__ import annotations

import contextlib
import logging
import os
import pwd
import shlex
import shutil
import subprocess
import traceback
from pathlib import Path
from typing import Any

import yaml
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import Submitted
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel

from .base import BaseTool, ToolContext, ToolResult, load_tool_config
from .shell_runner import run_bounded
from ..core.model_access import model_access_brief

# Observability imports (optional, graceful degradation if not available)
try:
    from shared.observability import get_publisher
    from shared.observability.events import EventType, Source
    from shared.observability.model_io import model_call, model_call_end
    OBSERVABILITY_AVAILABLE = True
except ImportError:
    OBSERVABILITY_AVAILABLE = False
    get_publisher = None  # type: ignore


# Sentinel for "the config did not mention this key at all", which is a different fact from an
# explicit null. Used for `set_cache_control`, where absent means "decide from the model name" and an
# explicit null means "the operator turned caching off on purpose".
_UNSET = object()


def _normalize_tool_calls(message: dict) -> list:
    """Best-effort normalize mini-swe's message-dump tool_calls -> [{name, arguments}]."""
    import json as _json
    out = []
    for tc in (message.get("tool_calls") or []):
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = _json.loads(args)
            except Exception:  # noqa: BLE001
                pass
        out.append({"name": fn.get("name"), "arguments": args})
    return out


def _abnormal_finish_reasons(agent: Any) -> "dict[str, int]":
    """Finish reasons behind the sub-agent's empty turns, counted, most-common signal first.

    A turn that carries no bash tool call is recorded by mini-swe as a format error, and enough in a
    row end the sub-task with `RepeatedFormatError`. That exit status says nothing about WHY the turns
    were empty: a real content-filter block (`finish_reason=content_filter`), a truncated response
    (`length`), or the model simply omitting the call all read the same. litellm persists the raw
    provider response on the format-error message it appends (under `extra.response`), so the finish
    reason is recoverable here without re-querying. Returns a mapping of finish_reason -> count for
    the reasons worth reporting (anything not a normal completion). Read entirely defensively: a shape
    surprise degrades to an empty mapping rather than raising, since this only annotates an error that
    already happened.
    """
    normal = {None, "", "stop", "end_turn", "tool_calls"}
    counts: dict[str, int] = {}
    for message in getattr(agent, "messages", None) or []:
        if not isinstance(message, dict):
            continue
        response = (message.get("extra") or {}).get("response")
        if not isinstance(response, dict):
            continue
        choices = response.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        reason = first.get("finish_reason")
        if reason in normal:
            continue
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


class InstrumentedModel(LitellmModel):
    """LitellmModel wrapper that records mini-swe's full model I/O as ModelCall trace objects.

    mini-swe runs inside the agent process (whose publisher is source APPSCI_AGENT), so we tag these
    calls explicitly as MINI_SWE_AGENT and parent them under the swe task span. Mirrors how
    InstrumentedEnvironment wraps LocalEnvironment. No-ops if observability is off."""

    def __init__(self, *args, parent_span_id: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._parent_span_id = parent_span_id

    def query(self, messages, **kwargs) -> dict:
        # `model_call` (not a bare start/end pair) because a RAISING query used to leave the
        # MODEL_REQUEST event orphaned: model_call_end never ran, so no ModelCall record was written
        # and the `messages` captured at start -- the entire mini-swe conversation up to that step --
        # were dropped. The tracer resolves a sub-agent's conversation from the *deepest* of its
        # model-call refs, which is precisely the failed one, so one dead step blanked the whole
        # expanded sub-agent view. Seen on run airline_rebooking_20260806_171519, span 2cb398ad: a
        # model.request for ref 0e0600c4 (n_messages=6) with no matching model.response and no entry
        # in model_calls-mini_swe_agent.jsonl. The guarantee now lives in `model_call` so every call
        # site gets it -- this one had the fix while agent_loop and the eval judge/user-sim did not.
        with (model_call(messages, model=self.config.model_name, caller="mini_swe",
                         source=Source.MINI_SWE_AGENT, name="mini-swe",
                         parent_span_id=self._parent_span_id)
              if OBSERVABILITY_AVAILABLE else contextlib.nullcontext(None)) as mc:
            result = super().query(messages, **kwargs)
            if mc is not None:
                # EVERYTHING IN THIS BLOCK IS INSTRUMENTATION, so it is wrapped: the contract in
                # shared/observability/README.md is that a trace write never fails the call it
                # measures. Without the guard, a shape change in litellm's response raises INSIDE the
                # `model_call` scope -- and that scope re-raises after writing a
                # `<model call failed: ...>` stub, so a model call that actually SUCCEEDED both kills
                # the mini-swe step and is recorded as a failure. The read is nested three deep
                # through an object we do not own, which is exactly where that risk lives.
                try:
                    # LitellmModel.query returns the *message* dict (choices[0].message) and hangs the
                    # raw completion plus its own cost calculation off `extra` -- there is no
                    # top-level "response" key. Reading result["response"] silently yielded None for
                    # every mini-swe call, so BUILD-phase tokens (the bulk of a run's spend) were
                    # never recorded.
                    extra = (result.get("extra") or {}) if isinstance(result, dict) else {}
                    response = extra.get("response") or {}
                    # `response` is a litellm ModelResponse, NOT a dict. It happens to support .get()
                    # in 1.88.1 (verified); attribute access is the fallback so a version that drops
                    # the mapping interface degrades to "no usage recorded" instead of raising.
                    usage = (
                        response.get("usage") if hasattr(response, "get")
                        else getattr(response, "usage", None)
                    )
                    # mini-swe already prices the call through litellm's cost calculator; 0.0 is its
                    # "couldn't price this model" sentinel, not a free call, so don't record it as one.
                    cost = extra.get("cost")
                    cost_usd = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
                    # Why the model stopped, read the same defensive way as `usage` above: through
                    # `.get()` when the object offers it, attribute access otherwise, and any shape
                    # surprise degrades to None rather than raising inside the instrumentation.
                    # "length" here means the sub-agent's own response was cut off mid-generation, so
                    # its bash command may be truncated -- worth seeing in the trace rather than
                    # inferring from a command that mysteriously does nothing.
                    finish_reason = None
                    try:
                        choices = (
                            response.get("choices") if hasattr(response, "get")
                            else getattr(response, "choices", None)
                        ) or []
                        if choices:
                            first = choices[0]
                            finish_reason = (
                                first.get("finish_reason") if hasattr(first, "get")
                                else getattr(first, "finish_reason", None)
                            )
                    except Exception:  # noqa: BLE001 -- a missing finish reason is not worth a failure
                        finish_reason = None
                    model_call_end(mc, content=result.get("content") if isinstance(result, dict)
                                   else None,
                                   tool_calls=_normalize_tool_calls(result), usage=usage,
                                   cost_usd=cost_usd, finish_reason=finish_reason)
                except Exception:  # noqa: BLE001 -- observability must never break the real call
                    logger.debug(
                        "mini-swe model-call instrumentation failed; the call itself succeeded",
                        exc_info=True,
                    )
                    # Still close the span as the success it was. Left open, `model_call` would have
                    # stubbed it as a failure on the way out -- and a call recorded as failed is worse
                    # than one recorded without usage, because the tracer resolves a sub-agent's
                    # conversation from its deepest ref and a failure stub blanks that view.
                    if not mc.closed:
                        try:
                            model_call_end(mc, content=None)
                        except Exception:  # noqa: BLE001 -- nothing left to try
                            logger.debug("could not close the mini-swe span", exc_info=True)
        return result

# Path to our appsci-bench specific config for mini-swe-agent
APPSCI_CONFIG_PATH = Path(__file__).parent.parent / "config" / "appsci_bench.yaml"

#: Cap on mini-swe's own retry attempts per model call (its default is 10). Deliberately small: the
#: failure this bounds is a gateway that will not accept the request at all, where more attempts only
#: burn the cell's time budget. See where it is applied for the arithmetic.
MINI_SWE_RETRY_ATTEMPTS = 3

logger = logging.getLogger(__name__)


def load_appsci_config() -> dict:
    """Load the appsci-bench configuration for mini-swe-agent."""
    if not APPSCI_CONFIG_PATH.exists():
        raise FileNotFoundError(f"AppSci config not found at {APPSCI_CONFIG_PATH}")
    with open(APPSCI_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# Directories that never contain meaningful agent edits. Pruning them keeps the
# per-step filesystem scan cheap and the file-write signal clean.
_IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea",
    ".next", "dist", "build",
}

# Safety cap: if the workspace tree is larger than this, skip the snapshot diff
# entirely (bash-command events are still emitted). Prevents pathological cost
# on huge checkouts.
_MAX_TRACKED_FILES = 20_000

# Cap file-write events emitted per step so a bulk operation (e.g. `pip install`,
# `npm install` that slips past the ignore list) can't flood the timeline.
_MAX_WRITES_PER_STEP = 50

# The unprivileged user every sub-agent command runs as, and its home. Must match the user created in
# appsci_agent/Dockerfile: uid 1001, home /home/swe.
SUB_AGENT_USER = "swe"
SUB_AGENT_HOME = "/home/swe"

# Environment variables removed from every sub-agent command. The sub-agent's job is to author the
# artifact's files; these are the harness's own plumbing, and each one is a way around a tool
# boundary rather than a capability the work needs:
#   APPSCI_USERSIM_*      -- reveals that the "customer" is a simulator, and lets the sub-agent
#                            interrogate it directly instead of through `talk_to_user`
#   ORCHESTRATOR_SUBMIT_URL, APPSCI_AGENT_ARTIFACT_NETWORK
#                         -- submit/deploy behind `deploy_artifact` and `complete`
#   OBSERVABILITY_*       -- trace plumbing; also what a sub-agent would need to append forged
#                            events to the run's mounted log files
#   PYTHONPATH            -- points at /app, which this user cannot read. Not secrecy: leaving it set
#                            makes every tool that WALKS sys.path (pip, importlib.metadata) die with
#                            `PermissionError: '/app'` instead of importing fine, so `pip install`
#                            fails for the sub-agent. Verified in the image. Nothing the sub-agent
#                            writes needs it; the agent process keeps it.
# DELIBERATELY KEPT: the LLM credentials (the sub-agent has to smoke-test the artifact's model client
# before deploying it) and the task-environment vars from spec.task_runner.agent_env, e.g. airline's
# AIRLINE_TOOLS_URL/TOKEN -- those ARE the task.
_SCRUBBED_ENV_VARS = (
    "APPSCI_USERSIM_URL",
    "APPSCI_USERSIM_TOKEN",
    "ORCHESTRATOR_SUBMIT_URL",
    "APPSCI_AGENT_ARTIFACT_NETWORK",
    "OBSERVABILITY_RUNS_DIR",
    "OBSERVABILITY_TRACE_ID",
    "OBSERVABILITY_TASK",
    "PYTHONPATH",
)


def can_drop_privileges() -> bool:
    """Can this process run the sub-agent's commands as ``SUB_AGENT_USER``?

    True only inside the agent image: we must be root (setuid is a privileged operation), the user
    must exist, and ``setpriv`` must be installed. Anywhere else -- a developer's laptop, CI, a unit
    test -- all three fail and commands run unchanged, because there is nothing to protect there: the
    boundary exists to keep the agent-under-test out of the harness's source inside the container.
    """
    if os.geteuid() != 0 or shutil.which("setpriv") is None:
        return False
    try:
        pwd.getpwnam(SUB_AGENT_USER)
    except KeyError:
        return False
    return True


def makedirs_writable_by_sub_agent(path: str) -> None:
    """Create ``path`` and hand it to ``SUB_AGENT_USER``, so sub-agent commands can write into it.

    Directories a tool creates for the sub-agent to write into are made by the AGENT process, which is
    root, while every sub-agent command runs as ``SUB_AGENT_USER``. The image chowns ``/workspace`` at
    build time, but that cannot cover a directory created at run time: it lands root-owned and, under
    the default umask, read-only to everyone else. A brief that then requires the sub-agent to write a
    file there asks for something the filesystem forbids, and the tool reads the resulting absence as
    "the sub-agent did not do the work" rather than "the sub-agent could not".

    Ownership is transferred rather than the mode widened, because world-writable would also let the
    task environment's own processes rewrite this tool's state.

    Never raises. Outside the agent image there is no separate user and nothing to transfer, and a
    chown that fails leaves the directory exactly as it is today -- the caller's own error handling
    still covers the write.
    """
    os.makedirs(path, exist_ok=True)
    if not can_drop_privileges():
        return
    try:
        entry = pwd.getpwnam(SUB_AGENT_USER)
        os.chown(path, entry.pw_uid, entry.pw_gid)
    except (KeyError, OSError):
        logger.warning("could not give %s to %s; sub-agent writes there may fail",
                       path, SUB_AGENT_USER, exc_info=True)


def build_sandboxed_command(command: str) -> str:
    """Wrap one sub-agent shell command so it runs scrubbed, and unprivileged where possible.

    The command is nested inside ``sh -c`` (via ``shlex.quote``) rather than reassembled, so heredocs,
    quotes, newlines, and ``$``-expansion behave exactly as they do today -- ``sh``, not ``bash``,
    because that is the interpreter mini-swe's commands already run under (``Popen(shell=True)``).

    Wrapping the command string is deliberate: it keeps the uid drop independent of the runner
    (``shell_runner.run_bounded``), which only has to start ``sh``, enforce the timeout, and collect the
    output, and it leaves the ``COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`` detection to upstream's
    ``LocalEnvironment._check_finished``.
    """
    prefix = ["env"]
    for name in _SCRUBBED_ENV_VARS:
        prefix += ["-u", name]

    if can_drop_privileges():
        # HOME must follow the uid. Left at root's, `pip install` finds neither a writable
        # site-packages nor a writable home and fails outright; pointed at the sub-agent's home it
        # falls back to a --user install that imports normally. git and tool caches need it too.
        prefix.append(f"HOME={SUB_AGENT_HOME}")
        prefix += ["setpriv", f"--reuid={SUB_AGENT_USER}", f"--regid={SUB_AGENT_USER}",
                   "--init-groups"]

    return " ".join(prefix) + f" /bin/sh -c {shlex.quote(command)}"


#: How much of a failed command is kept in ``command_errors``. Enough to recognise the command; the
#: full text is in the sub-agent's model-call record.
_COMMAND_ERROR_CHARS = 300

#: How many failed commands the tool lists for the main agent before summarising the rest as a count.
_COMMAND_ERRORS_LISTED = 5


def command_error_report(errors: list[dict[str, Any]]) -> str:
    """Plain text naming the sub-agent's commands that did not run to completion; empty if none."""
    if not errors:
        return ""
    lines = [f"{len(errors)} of the sub-agent's shell commands did not run to completion:"]
    for err in errors[:_COMMAND_ERRORS_LISTED]:
        if err["timed_out"]:
            what = f"timed out after {err['timeout_s']}s and was killed"
            if err["output_held_open"]:
                what += ("; a process it started outside its process group still held its output "
                         "open and may still be running")
        else:
            what = f"failed to run ({err['exception_type']})"
        lines.append(f"- {what}: {err['command']}")
    if len(errors) > _COMMAND_ERRORS_LISTED:
        lines.append(f"- and {len(errors) - _COMMAND_ERRORS_LISTED} more")
    return "\n".join(lines)


class SubAgentEnvironment(LocalEnvironment):
    """LocalEnvironment that runs each command as an unprivileged user, with harness vars scrubbed.

    The agent process is root and its own source sits at /app mode 0700, so a command running as
    ``SUB_AGENT_USER`` gets EACCES on the harness's code -- the LLM client and its gateway wiring, the
    user-sim client, the tool implementations. None of that is the task: the sub-agent is asked to
    author the artifact's files, and what it needs to know about model access is stated in its prompt
    (``appsci_agent/core/model_access.py``) instead of being discoverable by reading the harness.

    Outside the container none of the three preconditions in ``can_drop_privileges`` holds and
    commands run as-is; the scrub applies either way.

    Commands run through ``shell_runner.run_bounded`` rather than upstream's runner, whose wait after a
    timeout kill has no limit.

    ``command_errors`` records every command that did not run to completion (it timed out, or could
    not be started), so the tool can report them to the main agent alongside the sub-agent's own
    account. A command that ran and exited non-zero is not an error here: that is the command's
    result, and the sub-agent already sees it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_errors: list[dict[str, Any]] = []

    def execute(self, action, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Upstream's ``LocalEnvironment.execute``, with its runner swapped for ``run_bounded``.

        Upstream's runner waits without a limit after its timeout kill, so a command that leaves a
        process in another session holding its output pipe hangs the whole build (see
        ``shell_runner``). The result dict and the ``_check_finished`` submit detection are upstream's,
        unchanged.
        """
        model_command = action.get("command", "") if isinstance(action, dict) else str(action)
        # Wrapped here, not written back into ``action``: mini-swe keeps its own record of the action
        # it chose, and the event InstrumentedEnvironment emits should report the command the model
        # wrote, not this wrapper.
        command = build_sandboxed_command(model_command)
        cwd = cwd or self.config.cwd or os.getcwd()
        try:
            result = run_bounded(command, cwd, os.environ | self.config.env, timeout or self.config.timeout)
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except Exception as e:
            raw_output = getattr(e, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            timed_out = isinstance(e, subprocess.TimeoutExpired)
            output_held_open = bool(getattr(e, "output_held_open", False))
            output = {
                "output": raw_output,
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e),
                          "timed_out": timed_out, "output_held_open": output_held_open},
            }
            self.command_errors.append({
                "command": model_command[:_COMMAND_ERROR_CHARS],
                "exception_type": type(e).__name__,
                "timed_out": timed_out,
                "timeout_s": getattr(e, "timeout", None) if timed_out else None,
                "output_held_open": output_held_open,
            })
        self._check_finished(output)
        return output


class InstrumentedEnvironment(SubAgentEnvironment):
    """SubAgentEnvironment wrapper that emits observability events.

    Emits a ``swe.bash_command`` event per step, and detects file writes by
    diffing a snapshot of the workspace tree taken before and after each
    command. This catches *all* write mechanisms — Python scripts, editors,
    ``sed -i``, heredocs, ``git checkout``, etc. — not just shell redirection,
    and reports the actual paths touched plus whether each was created,
    modified, or deleted.
    """

    def __init__(self, cwd: str, env: dict, parent_span_id: str | None = None):
        super().__init__(cwd=cwd, env=env)
        self._parent_span_id = parent_span_id
        self._step_num = 0

    def execute(self, action, *args, **kwargs) -> Any:
        """Execute a command and emit observability events.

        ``action`` is mini-swe-agent's action dict (``{"command": ...}``); we
        forward ``*args``/``**kwargs`` verbatim so we stay compatible with the
        upstream signature.
        """
        self._step_num += 1

        command = action.get("command", "") if isinstance(action, dict) else str(action)

        publisher = get_publisher() if OBSERVABILITY_AVAILABLE else None
        if publisher:
            publisher.emit(
                event_type=EventType.SWE_BASH_COMMAND,
                source=Source.MINI_SWE_AGENT,
                name="mini-swe",
                parent_span_id=self._parent_span_id,
                payload={
                    "step_num": self._step_num,
                    "command": command[:500],  # Truncate long commands
                },
            )

        # Snapshot before running so we can diff for file changes afterwards.
        before = self._snapshot_tree() if publisher else None
        result: dict[str, Any] | None = None
        try:
            result = super().execute(action, *args, **kwargs)
            return result
        except Submitted:
            result = {"returncode": 0, "submitted": True}
            raise
        finally:
            if publisher is not None:
                try:
                    self._emit_result(publisher, result)
                except Exception:
                    pass  # Observability is best-effort; never break the agent loop.
            # Runs even when the command triggers a Submitted exception, so the
            # final step's writes are still captured.
            if publisher is not None and before is not None:
                try:
                    self._emit_file_changes(publisher, before)
                except Exception:
                    # Observability is best-effort; never break the agent loop.
                    pass

    def _emit_result(self, publisher, result: dict[str, Any] | None) -> None:
        """Record how the step's command ended, beside the ``swe.bash_command`` that started it.

        The trace used to record only the command, so a step that timed out, or never returned, looked
        the same as one that succeeded. ``result`` is None when ``execute`` raised something other than
        a submission, which upstream's contract says it does not.
        """
        extra = (result or {}).get("extra") or {}
        publisher.emit(
            event_type=EventType.SWE_BASH_RESULT,
            source=Source.MINI_SWE_AGENT,
            name="mini-swe",
            parent_span_id=self._parent_span_id,
            payload={
                "step_num": self._step_num,
                "returncode": (result or {}).get("returncode"),
                "submitted": bool((result or {}).get("submitted")),
                "timed_out": bool(extra.get("timed_out")),
                "output_held_open": bool(extra.get("output_held_open")),
                "exception_type": extra.get("exception_type"),
                "raised": result is None,
            },
        )

    def _snapshot_tree(self) -> dict[str, tuple[int, int]] | None:
        """Map each file under the working dir to (mtime_ns, size).

        Returns None if the tree is too large or unreadable, which disables
        file-write detection for that step.
        """
        root = self.config.cwd or os.getcwd()
        snapshot: dict[str, tuple[int, int]] = {}
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune ignored dirs in place so os.walk doesn't descend them.
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _IGNORE_DIRS and not d.endswith(".egg-info")
                ]
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue  # File vanished mid-scan; ignore.
                    snapshot[os.path.relpath(full, root)] = (st.st_mtime_ns, st.st_size)
                    if len(snapshot) > _MAX_TRACKED_FILES:
                        return None
        except OSError:
            return None
        return snapshot

    def _emit_file_changes(
        self, publisher, before: dict[str, tuple[int, int]]
    ) -> None:
        """Diff a fresh snapshot against ``before`` and emit file-write events."""
        after = self._snapshot_tree()
        if after is None:
            return

        changes: list[tuple[str, str]] = []
        for path, meta in after.items():
            if path not in before:
                changes.append((path, "created"))
            elif before[path] != meta:
                changes.append((path, "modified"))
        for path in before:
            if path not in after:
                changes.append((path, "deleted"))

        if not changes:
            return

        for path, change_type in changes[:_MAX_WRITES_PER_STEP]:
            publisher.emit(
                event_type=EventType.SWE_FILE_WRITE,
                source=Source.MINI_SWE_AGENT,
                name="mini-swe",
                parent_span_id=self._parent_span_id,
                payload={
                    "step_num": self._step_num,
                    "path": path,
                    "change_type": change_type,
                },
            )

        if len(changes) > _MAX_WRITES_PER_STEP:
            publisher.emit(
                event_type=EventType.SWE_FILE_WRITE,
                source=Source.MINI_SWE_AGENT,
                name="mini-swe",
                parent_span_id=self._parent_span_id,
                payload={
                    "step_num": self._step_num,
                    "truncated": True,
                    "total_changes": len(changes),
                    "shown": _MAX_WRITES_PER_STEP,
                },
            )


def _missing_task_error(task_context: str) -> str:
    """Build the rejection message for a call that omitted the required ``task``.

    This covers a call that genuinely arrived without the field. A call whose arguments were cut off
    at the output token limit is a different failure with a different fix ("write less", not "fill in
    the field"), and it is caught one level up in ``core/agent.py`` -- which is where the finish reason
    is known -- rather than being misreported here. Telling a truncated call that it forgot a field is
    what turned one truncation into 37 dead turns in
    order_to_cash_claude-sonnet-4-6_b1_l3_20260831_074504.
    """
    base = (
        "Invalid tool call: 'task' is required and was missing or empty. "
        "'task' is the work to perform; 'context' is optional supporting detail and is "
        "NOT a substitute for it."
    )
    if task_context:
        # The observed failure: the whole brief arrived in `context`, `task` absent. Say so
        # explicitly and tell the model exactly how to re-send -- the fix is a field move, not
        # rewriting the content.
        return (
            f"{base} You sent only 'context' ({len(task_context)} chars) and no 'task'. "
            "Re-send this call with that text in 'task' -- you can move it verbatim -- keeping "
            "only genuine background (what you've already learned) in 'context'."
        )
    return (
        f"{base} You sent neither field. Re-send this call with 'task' set to a clear, "
        "specific description of the coding/shell work to perform."
    )


def resolve_task_args(params: dict[str, Any]) -> tuple[str, str, str | None]:
    """Validate the tool's (task, context) arguments.

    Returns ``(task, context, error)`` -- ``error`` is None when the call is valid.

    The tool schema marks ``task`` as required, but nothing enforces that at the API boundary
    (the function definition carries no ``strict`` flag, and the provider does not validate
    tool arguments against the schema), so an invalid call can still arrive. Observed failure
    mode: because ``task`` and ``context`` are both free-text and describe overlapping things,
    the model packed the entire brief into ``context`` and omitted ``task`` -- valid, complete
    JSON, but an invalid call.

    Such a call is **rejected**, not repaired. Promoting ``context`` to ``task`` would guess at
    intent and would erase the malformed call from the trace, hiding a real agent defect behind
    an apparently-successful tool invocation. Instead the rejection names the missing field and
    says exactly how to re-send, so the model can self-correct in one turn.
    """
    task = (params.get("task") or "").strip()
    task_context = (params.get("context") or "").strip()

    if not task:
        return "", "", _missing_task_error(task_context)

    return task, task_context, None


class MiniSweAgentTool(BaseTool):
    """Delegate coding and shell tasks to a specialized subagent."""

    def __init__(self):
        self._config = load_tool_config("mini_swe_agent")

    @property
    def name(self) -> str:
        return self._config["name"]

    @property
    def description(self) -> str:
        return self._config["description"]

    @property
    def parameters(self) -> dict[str, Any]:
        return self._config["parameters"]

    def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the mini_swe_agent tool.

        Delegates the task to mini-swe-agent and returns the result.
        """
        task, task_context, arg_error = resolve_task_args(params)

        if arg_error:
            # `invalid_call` so the agent loop can tell "this call never ran" from "the work ran and
            # failed". Only the former means the agent has stopped making contact with the environment,
            # and only the former should count toward the loop's stuck-run breaker.
            return ToolResult(
                success=False,
                output={},
                error=arg_error,
                invalid_call=True,
            )

        # Build the full task description with context
        full_task = task
        if task_context:
            full_task = f"{task}\n\nContext: {task_context}"

        # Add workspace information
        full_task = f"{full_task}\n\nWorkspace directory: {context.workspace_dir}\nArtifact directory: {context.artifact_dir}"

        # State how the artifact reaches a model (variable names only, never values). Without this the
        # sub-agent's only route to those facts was reading the harness's own source, which is both
        # off-task and -- since the commands run as an unprivileged user and /app is root-only -- no
        # longer possible.
        if context.config:
            brief = model_access_brief(context.config)
            if brief:
                full_task = f"{full_task}\n\n{brief}"

        # Get observability publisher and emit task start event
        publisher = get_publisher() if OBSERVABILITY_AVAILABLE else None
        task_span_id = None
        if publisher:
            task_span_id = publisher.emit(
                event_type=EventType.SWE_TASK_START,
                source=Source.MINI_SWE_AGENT,
                name="mini-swe",
                payload={"task": task[:500]},
                parent_span_id=getattr(context, "parent_span_id", None),
            )

        try:
            # Get model from config if available
            model_name = "claude-opus-4-8"
            max_turns = 30
            if context.config:
                model_name = getattr(context.config, "mini_swe_model", model_name)
                max_turns = getattr(context.config, "mini_swe_max_turns", max_turns)

            # Load appsci-bench specific config for mini-swe-agent
            appsci_config = load_appsci_config()

            # Build agent config from appsci config
            agent_config = appsci_config.get("agent", {})
            agent_config["step_limit"] = max_turns

            # Build model config
            model_config = appsci_config.get("model", {})

            # Merge our auth kwargs with the config's model_kwargs
            model_kwargs = model_config.get("model_kwargs", {}).copy()
            if context.config and context.config.api_key:
                model_kwargs["api_key"] = context.config.api_key
            if context.config and context.config.base_url:
                model_kwargs["base_url"] = context.config.base_url
            # Resilience: let litellm retry transient errors (gateway Timeout, RateLimit, connection
            # blips) with exponential backoff instead of failing the whole subagent task on a single
            # hiccup. litellm only auto-selects backoff for RateLimitError; a generic Timeout/APIError
            # would otherwise retry at a flat interval, so set the strategy explicitly.
            model_kwargs.setdefault("num_retries", 3)
            model_kwargs.setdefault("retry_strategy", "exponential_backoff_retry")
            # Bound mini-swe's OWN retry ladder, which sits above litellm's and defaults to 10
            # attempts with `wait_exponential(min=4, max=60)`. Two nested ladders multiply, and on a
            # request the gateway will never accept the product is the whole failure:
            #
            #   4 litellm attempts x ~60s (gateway hangs up after ~60s with no bytes) = ~244s
            #   x 10 tenacity attempts + backoff (4,4,4,8,16,32,60,60,60)             = ~49 min
            #
            # That is measured, not hypothetical: an `eval_generator` call ran 04:23:49 -> 05:20 that
            # way and failed anyway, having consumed most of the cell's budget for one doomed request.
            # Retrying is still worth doing -- across four order_to_cash runs every call that crossed
            # the ceiling was rescued by a retry -- so this caps the ladder rather than removing it,
            # trading a 49-minute stall for a ~12-minute worst case and a legible failure.
            #
            # Set via env because that is the only knob mini-swe exposes (its `retry()` helper reads
            # this name at query time; there is no config field). `setdefault` so an operator who has
            # deliberately exported a different value keeps it.
            os.environ.setdefault("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", str(MINI_SWE_RETRY_ATTEMPTS))
            # Gateway identity/attribution headers (e.g. X-Gateway-User), when configured. Set only
            # when non-empty so a non-gateway run's requests are unchanged.
            if context.config and getattr(context.config, "custom_headers", None):
                model_kwargs["extra_headers"] = dict(context.config.custom_headers)
            # Explicit per-request timeout so a hung/throttled gateway call fails fast (and the retry
            # loop cycles) instead of parking on litellm's 6000s default. Tunable via env.
            model_kwargs.setdefault("timeout", float(os.getenv("APPSCI_LLM_TIMEOUT_S", "120")))
            # BUILD-side sampling temperature, when the run asked for one. THIS is the call that
            # matters for build replicates: mini-swe does the implementation work and was 99.7% of the
            # first smoke sweep's token use, so a temperature applied only to the interviewing agent
            # above would barely move what a rebuild produces.
            #
            # Set only when present, so an unset temperature leaves the request byte-identical to
            # every run before this knob existed. `drop_params: true` is already in the config below,
            # so a model that refuses the parameter has litellm drop it rather than fail the build.
            if context.config and getattr(context.config, "temperature", None) is not None:
                model_kwargs["temperature"] = context.config.temperature

            # Build environment config
            env_config = appsci_config.get("environment", {})

            # Create model with appsci-bench config. Use the instrumented model when observability is
            # on so mini-swe's full LLM I/O is captured as ModelCall trace objects (tagged MINI_SWE_AGENT).
            model_cls = InstrumentedModel if (OBSERVABILITY_AVAILABLE and publisher) else LitellmModel
            model_extra = {"parent_span_id": task_span_id} if model_cls is InstrumentedModel else {}

            # Prompt caching. Anthropic caching is explicit opt-in -- unmarked content is not cached,
            # and the provider will not do it for us (OpenAI's automatic caching is the exception, not
            # the rule). mini-swe DOES default this on for Claude models, but only inside its
            # `get_model()` factory; constructing the model class directly, as we must in order to
            # substitute the instrumented subclass, silently drops that default.
            #
            # The cost of the drop was the whole point of the $5-cap failures: cache reads were zero
            # across all 823 sub-agent calls of
            # airline_rebooking_claude-sonnet-4-6_b0_l3_20260831_083903, so a prompt growing to ~60k
            # tokens was re-billed in full every step.
            #
            # Mirrors the factory's own condition rather than setting it unconditionally: the marker is
            # an Anthropic construct, and sending it to a model that does not take it is a needless
            # request change. An explicit `set_cache_control` in the config block still wins -- and is
            # passed through here, since nothing else in this constructor forwards it.
            cache_mode = model_config.get("set_cache_control", _UNSET)
            if cache_mode is _UNSET:
                cache_mode = (
                    "default_end"
                    if any(s in model_name.lower() for s in ("anthropic", "sonnet", "opus", "claude"))
                    else None
                )
            if cache_mode is not None:
                model_extra["set_cache_control"] = cache_mode

            model = model_cls(
                model_name=model_name,
                model_kwargs=model_kwargs,
                format_error_template=model_config.get("format_error_template", "{{ error }}"),
                observation_template=model_config.get("observation_template", ""),
                **model_extra,
            )

            # Create environment with workspace dir and env vars from config.
            # Either way the sub-agent's commands are sandboxed (SubAgentEnvironment); the
            # instrumented subclass adds the observability events on top.
            if OBSERVABILITY_AVAILABLE and publisher:
                env = InstrumentedEnvironment(
                    cwd=context.workspace_dir,
                    env=env_config.get("env", {}),
                    parent_span_id=task_span_id,
                )
            else:
                env = SubAgentEnvironment(
                    cwd=context.workspace_dir,
                    env=env_config.get("env", {}),
                )

            # Create and run the agent with config from default.yaml
            agent = DefaultAgent(model=model, env=env, **agent_config)
            result = agent.run(full_task)

            # Parse the result - mini-swe-agent returns:
            # {"exit_status": "...", "submission": "...", "exception_str": "...", "traceback": "..."}
            exit_status = result.get("exit_status", "")
            submission = result.get("submission", "")

            # Only "Submitted" is a success - everything else is a failure
            is_success = exit_status == "Submitted"

            # Emit task complete event
            if publisher:
                step_count = env._step_num if hasattr(env, '_step_num') else 0
                publisher.emit(
                    event_type=EventType.SWE_TASK_COMPLETE,
                    source=Source.MINI_SWE_AGENT,
                    name="mini-swe",
                    span_id=task_span_id,
                    payload={
                        "success": is_success,
                        "exit_status": exit_status,
                        "step_count": step_count,
                        "command_errors": len(env.command_errors),
                        "commands_timed_out": sum(e["timed_out"] for e in env.command_errors),
                        "commands_output_held_open": sum(e["output_held_open"]
                                                         for e in env.command_errors),
                    },
                )

            # Reported whatever the sub-agent says in its submission: it may not mention a command
            # that timed out, and a process left holding a command's output may still be running.
            command_errors = command_error_report(env.command_errors)

            if is_success:
                output = {"status": "completed", "result": submission}
                if command_errors:
                    output["command_errors"] = command_errors
                return ToolResult(success=True, output=output)
            else:
                # Build descriptive error message
                error_parts = [f"mini-swe-agent failed with exit_status: {exit_status}"]
                # `RepeatedFormatError` alone does not say why the sub-agent's turns were empty. When
                # the underlying finish reasons are recoverable, report them plainly -- a run of
                # `content_filter` (the provider blocked the response) is a different problem from a
                # run of `length` (responses truncated), and the meta-agent can only adapt if it is
                # told which. No interpretation here; just surface the signal the LLM calls returned.
                finish_reasons = _abnormal_finish_reasons(agent)
                if finish_reasons:
                    summary = ", ".join(f"{reason} x{count}" for reason, count in finish_reasons.items())
                    error_parts.append(
                        "The sub-agent's responses ended without a command; the model calls "
                        f"reported finish_reason: {summary}."
                    )
                if command_errors:
                    error_parts.append(command_errors)
                if result.get("exception_str"):
                    error_parts.append(f"Exception: {result['exception_str']}")
                if result.get("traceback"):
                    error_parts.append(f"Traceback:\n{result['traceback']}")

                return ToolResult(
                    success=False,
                    output={
                        "status": "failed",
                        "exit_status": exit_status,
                        "submission": submission,
                    },
                    error="\n".join(error_parts),
                )

        except Exception as e:
            # Capture the full traceback: str(e) alone (e.g. "unhashable type:
            # 'slice'") loses the frame where the error was raised, which is
            # exactly what's needed to diagnose failures buried inside
            # mini-swe-agent / litellm. Log it (lands in the agent container
            # logs the orchestrator captures) and surface it to the caller.
            tb = traceback.format_exc()
            logger.exception("mini_swe_agent failed for task %r", task[:200])

            # Emit task complete event on error
            if publisher:
                # The publisher truncates payload strings from the front, so send
                # the tail of the traceback -- the raising frame + error message
                # are what matter, and they live at the bottom.
                publisher.emit(
                    event_type=EventType.SWE_TASK_COMPLETE,
                    source=Source.MINI_SWE_AGENT,
                    name="mini-swe",
                    span_id=task_span_id,
                    payload={
                        "success": False,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "traceback_tail": tb[-480:],
                    },
                )

            return ToolResult(
                success=False,
                output={"task": task},
                error=f"Error executing mini_swe_agent: {str(e)}\n\nTraceback:\n{tb}",
            )

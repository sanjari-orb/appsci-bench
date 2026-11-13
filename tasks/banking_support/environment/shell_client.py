"""Workspace-side driver for the `shell` tool: everything upstream-visible, none of the execution.

The tool surface the agent sees is defined by upstream's SandboxManager.run_command, so this
reproduces its observable contract exactly while the actual command runs in the sidecar container
(shell_sidecar/, which holds the corpus and nothing else -- see its Dockerfile for why):

  * the escape-pattern pre-check, with upstream's error wording, before anything is executed;
  * upstream's output sanitisation, so `ls -l` does not leak host metadata or vary per machine;
  * (exit_code, stdout, stderr), the shape ShellMixin.shell already knows how to render;
  * a timeout reported as exit 124, matching `timeout(1)` and upstream.

WHY THE ESCAPE CHECK SURVIVES even though it is no longer load-bearing. Upstream needed it because its
sandbox could not restrict reads at all (its own module notes reads are "deny-only", so kb_dir could not
be enforced) -- string-matching `..`, `~` and absolute paths was the only thing standing between the
agent and the rest of the filesystem. Our confinement does not depend on it: the sidecar has no secrets
to reach. But the check is AGENT-VISIBLE -- a command containing `..` comes back with a specific refusal
string, and the models on the published leaderboard were shaped by receiving it. Dropping it would
silently widen the tool relative to the runs we are trying to be comparable to, so it stays as fidelity,
not as defence.

WHY WE ARE NOT USING UPSTREAM'S RUNTIME. Its sandbox shells out to Anthropic's sandbox-runtime, which
needs bubblewrap and therefore user namespaces; in a stock task container that fails at construction
(`bwrap: No permissions to create a new namespace`), which is why `shell` reported "Sandbox not
initialized" in every run to date. Making it work would need SYS_ADMIN or relaxed seccomp -- weakening
the container in order to sandbox inside it. A separate, contentless container is both safer and
actually runnable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Fixed identity substituted into `ls -l` output. Upstream replaces the real user/group/date to avoid
# leaking host metadata; we keep the substitution because it ALSO makes the output deterministic --
# otherwise file mtimes (which come from image build time) would differ between builds, giving the agent
# a different `ls -l` for the same corpus.
_SANDBOX_USER = "kb_user"
_SANDBOX_GROUP = "kb_group"
_SANDBOX_DATE = "Jan  1 00:00"

_LS_LINE = re.compile(
    r"^([d\-lrwxst@]+\s+\d+\s+)"          # permissions + link count
    r"(\S+)\s+"                            # user
    r"(\S+)\s+"                            # group
    r"(\d+)\s+"                            # size
    r"(\w{3}\s+\d+\s+[\d:]+)\s+"          # date/time
    r"(.*)$",                              # filename
    re.MULTILINE,
)


def _has_escape_pattern(command: str) -> Optional[str]:
    """Upstream's pre-execution check, reproduced. Returns the offending pattern, or None.

    Quoted spans are blanked first so a document name containing one of these characters is not
    mistaken for an escape attempt -- upstream's own ordering.
    """
    stripped = re.sub(r"'[^']*'", '""', command)
    stripped = re.sub(r'"[^"]*"', '""', stripped)

    if ".." in stripped:
        return ".."
    if re.search(r"(?:^|[\s;|&])(/[a-zA-Z])", stripped):
        full = re.search(r"(?:^|[\s;|&])(/[a-zA-Z][^\s;|&]*)", stripped)
        if full:
            return f"absolute path: {full.group(1)}"
    if stripped.strip().startswith("/"):
        return "absolute path at start"
    if "~" in stripped:
        return "~"
    if "$HOME" in stripped or "${HOME}" in stripped:
        return "$HOME"
    return None


def _sanitize_output(output: str) -> str:
    """Normalize `ls -l` lines to a fixed user/group/date. Mirrors upstream's sanitizer, minus its
    host-home-directory rewrite: the sidecar has no host paths to rewrite."""
    if not output:
        return output

    def repl(m: "re.Match[str]") -> str:
        return f"{m.group(1)}{_SANDBOX_USER}  {_SANDBOX_GROUP}  {m.group(4)} {_SANDBOX_DATE} {m.group(6)}"

    return _LS_LINE.sub(repl, output)


class RemoteShell:
    """Drop-in replacement for upstream's SandboxManager, as far as `ShellMixin` is concerned.

    ShellMixin only ever calls `run_command` and reads the 3-tuple, so nothing about the tool's
    rendering changes -- including its quirk of turning grep's exit 1 into "No matches found."
    """

    def __init__(self, url: str, token: str = "", timeout: int = 35) -> None:
        # The client deadline is deliberately LONGER than the sidecar's own command timeout, so a slow
        # command is reported by the sidecar as exit 124 with upstream's wording rather than surfacing
        # here as a transport failure the agent cannot interpret.
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def health(self) -> bool:
        try:
            req = urllib.request.Request(f"{self._url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read()).get("exists", False)
        except Exception:
            return False

    def run_command(self, command: str, timeout: int | None = None) -> Tuple[int, str, str]:
        """Execute `command` in the sidecar; return (exit_code, stdout, stderr). Never raises.

        `timeout` is accepted for signature compatibility with the object this replaces; the effective
        ceiling is the sidecar's, since that is where the process actually runs.
        """
        del timeout
        pattern = _has_escape_pattern(command)
        if pattern:
            return (1, "",
                    f"Error: Command blocked - contains '{pattern}' which could escape the sandbox")

        body = json.dumps({"command": command}).encode()
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(f"{self._url}/run", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.URLError as exc:
            # The sidecar is down or unreachable. Report it as a command failure rather than raising:
            # a dead shell should degrade the run, not abort the case mid-conversation.
            logger.warning("shell sidecar unreachable at %s: %s", self._url, exc)
            return (1, "", f"Error: shell backend unavailable ({exc})")
        except Exception as exc:  # noqa: BLE001
            logger.warning("shell sidecar call failed: %s", exc)
            return (1, "", f"Error: shell backend failure ({exc})")

        return (int(payload.get("exit_code", 1)),
                _sanitize_output(payload.get("stdout", "")),
                _sanitize_output(payload.get("stderr", "")))


def shell_from_env() -> Optional[RemoteShell]:
    """Build a RemoteShell from the environment, or None when no sidecar is configured.

    None is the honest "this deployment has no shell" answer and keeps the pre-sidecar behaviour intact:
    ShellMixin reports the tool uninitialized while bm25/dense/grep carry on. Absence is NOT probed for
    liveness here -- a sidecar that is merely slow to boot must not permanently disable the tool for the
    whole run, so reachability is decided per call.
    """
    url = os.environ.get("BANKING_SHELL_URL", "").strip()
    if not url:
        return None
    return RemoteShell(url, token=os.environ.get("BANKING_SHELL_TOKEN", "").strip())

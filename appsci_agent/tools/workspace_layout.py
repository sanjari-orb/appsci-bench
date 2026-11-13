"""Where this layer's own files live under the workspace, and who may write to each.

ONE NAMESPACE, NOT A SCATTER. `eval_handoff.md`, the score history, the pending-change record and the
roll-back snapshots are all this layer's bookkeeping rather than anything the agent was asked to
build, and they used to sit loose in `/workspace` beside the harness. "Workspace" already means
several things to a reader -- the agent's scratch space, the directory its commands start in -- so
loose files there read as part of the task. They are namespaced under one dotted directory instead.

OWNERSHIP IS THE OTHER HALF, and it is why this is a tree rather than one directory. The agent process
is root; every sub-agent command runs as an unprivileged user. A directory the sub-agent owns can be
RENAMED by it -- renaming needs write permission on the PARENT, not on the directory -- so anything
the sub-agent must write has to be a leaf whose parent is root-owned:

    <workspace>/.appsci/                  root      the namespace; root-owned so neither child
                                                    below can be renamed out from under the tool
    <workspace>/.appsci/handoff/          SUB-AGENT eval_handoff.md, written by mini_swe_agent
    <workspace>/.appsci/optimize/         root      score history, pending change, snapshots, and
                                                    the report the tool writes; world-READABLE, so
                                                    the sub-agent can read the report it is given
    <workspace>/.appsci/optimize/inbox/   SUB-AGENT change_note.md, the one thing it writes back

The narrowing matters. Handing the sub-agent the whole optimize directory -- which is what a single
chown did -- granted unlink and rename on `score_history.json`, `pending_attempt.json` and
`snapshots/`, so the actor the harness-fingerprint check exists to police could delete the roll-back
copy of the candidate it had just been told not to keep.

BUT THE TREE ALONE IS NOT ENOUGH, and an earlier version of this file claimed it was. `/workspace`
itself used to be chowned to the sub-agent in the image, and it is baked in rather than bind-mounted
-- so a root-owned `.appsci` sitting inside it was still one `mv` away, by the very rule stated two
paragraphs up. What closes it is `/workspace` being root-owned, world-writable and STICKY (see
`appsci_agent/Dockerfile`): the sub-agent can still create its own files there, and cannot rename or
delete entries it does not own. Root ownership of `/workspace` is load-bearing for that, because the
sticky bit exempts a directory's own owner. `ensure_layout` reconciles ownership on every call rather
than only creating, so a node that ended up sub-agent-owned by some other route does not stay that
way for the rest of the run.

`/workspace` is copied out of the container at the end of a run (`_AGENT_SNAPSHOT_DIRS` in
`appsci_bench/core/agent_container.py`), so everything here still reaches the run record.
"""
from __future__ import annotations

import logging
import os

from .mini_swe_agent import makedirs_writable_by_sub_agent

logger = logging.getLogger(__name__)

#: This layer's namespace under the workspace. Root-owned.
APPSCI_DIRNAME = ".appsci"
#: Where the agent writes its eval handoff. Sub-agent-writable.
HANDOFF_DIRNAME = os.path.join(APPSCI_DIRNAME, "handoff")
#: `optimize_agent`'s own state. Root-owned, world-readable.
#:
#: Kept as a workspace-RELATIVE path with separators in it, so anything that joins it onto a workspace
#: directory keeps working as the layout moves.
OPTIMIZE_DIRNAME = os.path.join(APPSCI_DIRNAME, "optimize")
#: The only thing the cycle sub-agent writes back. Sub-agent-writable.
NOTE_INBOX_DIRNAME = os.path.join(OPTIMIZE_DIRNAME, "inbox")


def ensure_layout(workspace_dir: str) -> None:
    """Create the tree with the right owner on each node. Idempotent, and never raises.

    Called by whichever tool gets there first rather than once at start-up, so a tool is never
    depending on an ordering nobody enforces. Creating the root-owned parents FIRST is the part that
    matters: if the sub-agent's own `mkdir -p` wins the race it owns the whole chain, and then it can
    rename the tool's state directory -- which is exactly the hole this layout closes.
    """
    for relative, writable in ((APPSCI_DIRNAME, False), (OPTIMIZE_DIRNAME, False),
                               (HANDOFF_DIRNAME, True), (NOTE_INBOX_DIRNAME, True)):
        path = os.path.join(workspace_dir, relative)
        try:
            if writable:
                makedirs_writable_by_sub_agent(path)
            else:
                os.makedirs(path, exist_ok=True)
                # RECONCILED, not just created. `exist_ok` says nothing about who owns what is already
                # there, so a directory that ended up sub-agent-owned by any route would stay that way
                # for the rest of the run and every call would read the tree as fine.
                _reclaim(path)
        except OSError as e:
            # Logged, not raised. Callers invoke this defensively from several entry points, and a
            # workspace this process cannot write to is not a condition any of them can fix -- the
            # failure belongs to whatever then tries to write a file, which reports it with the
            # context of what it was writing.
            logger.warning("could not create %s under %s: %s", relative, workspace_dir, e)


def _reclaim(path: str) -> None:
    """Put a node this tool must own back under root. Never raises.

    Only meaningful inside the agent image, where there is a second user to have taken it; off the
    image `geteuid` is not 0 and there is nothing to reclaim.
    """
    if os.geteuid() != 0:
        return
    try:
        if os.stat(path).st_uid != 0:
            logger.warning("%s was not root-owned; reclaiming it", path)
            os.chown(path, 0, 0)
    except OSError as e:
        logger.warning("could not reclaim %s: %s", path, e)


def handoff_dir(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, HANDOFF_DIRNAME)


def note_inbox_dir(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, NOTE_INBOX_DIRNAME)

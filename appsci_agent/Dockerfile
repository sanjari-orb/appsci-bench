# AppSci Agent Docker image
#
# This container runs the appsci-agent that builds artifacts based on user
# requirements gathered from the appsci agent user sim. It has NO access to the
# Docker daemon: the agent authors its artifact (Dockerfile + server entry) and
# the host builds and runs it via the deploy_artifact tool, so neither the docker
# CLI nor the socket is installed or mounted here.
#
# TWO USERS, ON PURPOSE. The agent process runs as root and its own code lives at /app, mode 0700.
# The coding sub-agent (mini-swe) runs every bash command as the unprivileged `swe` user, so it can
# write the work dirs but cannot read /app. Without that split the subject under test reads the
# harness -- the meta-agent's LLM client, its gateway wiring, the user-sim client -- none of which is
# part of the task it was asked to implement. See appsci_agent/tools/mini_swe_agent.py
# (SubAgentEnvironment) for the command wrapper, and appsci_agent/README.md for the whole boundary.
#
# Build from repo root:
#   docker build -f appsci_agent/Dockerfile -t appsci-bench-agent .
#
# Run:
#   docker run -d --name appsci-agent-{task_id} \
#     --network bench-network \
#     -v /path/to/spec.yaml:/spec/spec.yaml:ro \
#     -e APPSCI_USERSIM_URL=http://usersim-{task_id}:8000 \
#     -e APPSCI_USERSIM_TOKEN=<token> \
#     -e TASK_WORKSPACE=<workspace-container> \
#     -e TASK_SPEC_PATH=/spec/spec.yaml \
#     -e ORCHESTRATOR_SUBMIT_URL=http://host.docker.internal:9000/submit \
#     -e ANTHROPIC_API_KEY=<key> \
#     appsci-bench-agent

FROM python:3.11-slim

# Install system dependencies. No docker CLI: the agent does not (and cannot) drive the daemon --
# deployment is a host-side service reached over HTTP via the deploy_artifact tool.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# The unprivileged user every mini-swe command runs as. The uid is FIXED (1001) because the
# orchestrator has to make the host-side artifact bind-mount writable for it before the container
# starts (appsci_bench/core/agent_container.py) -- a uid picked by useradd would leave that mount
# unwritable on Linux hosts. `setpriv` (util-linux) is what performs the drop; it ships in the base
# image, and the check below fails the BUILD rather than letting a future base image silently leave
# the sub-agent running as root.
RUN useradd --create-home --uid 1001 swe && command -v setpriv

WORKDIR /app

# Copy pyproject.toml first
COPY pyproject.toml /app/

# Copy source packages. The agent depends only on `shared` (the artifact contract + observability)
# and its own code -- deliberately NOT appsci_bench, so the evaluator (eval/) and env internals never
# ship in the agent-under-test image.
COPY appsci_agent /app/appsci_agent/
COPY shared /app/shared/

# Install the package (now with source available)
RUN pip install --no-cache-dir -e .

# Root-only from here on. The agent process is root, so its own imports are unaffected; the mini-swe
# sub-agent is uid 1001 and gets EACCES on every path under /app. PYTHONPATH below still names /app
# for the agent process, and is stripped from the sub-agent's commands -- an unreadable sys.path entry
# is not merely skipped: pip and importlib.metadata walk sys.path and raise on it. Applied AFTER the
# editable install because pip writes .egg-info into this tree.
RUN chmod 0700 /app

# Create working directories, owned by the sub-agent that writes them. /work/out is usually replaced
# at runtime by a bind mount from the host (the orchestrator's per-run artifact dir), so this
# ownership only covers the unmounted case; the mounted case is handled host-side.
RUN mkdir -p /work/out /workspace /spec \
    && chown -R swe:swe /work
# /workspace is the exception: root-owned, world-writable, STICKY -- the /tmp model.
#
# The sub-agent works here and must be able to create its own files, which world-writable gives it.
# What it must NOT be able to do is rename or delete entries it does not own, because this tool's
# state directory lives here: the score history, the pending-change record and the roll-back
# snapshots. Owning /workspace was enough to `mv` that directory aside and rebuild it, which is the
# hole a root-owned subdirectory alone does NOT close -- renaming an entry needs write permission on
# the PARENT, not on the entry. The sticky bit is what actually closes it, and it has to be paired
# with root ownership of /workspace: sticky exempts the directory's owner, so a swe-owned /workspace
# would still let swe remove root's entries.
RUN chown root:root /workspace && chmod 1777 /workspace

# Set working directory for agent operations
WORKDIR /work

# Set PYTHONPATH to ensure modules are found
ENV PYTHONPATH=/app

# Expose port for artifact serving (after build)
EXPOSE 8080

# Run the agent
CMD ["python", "-m", "appsci_agent.run"]

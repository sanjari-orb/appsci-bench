# The banking `shell` tool's sidecar image -- a shell, the knowledge corpus, and nothing else.
#
# WHY A SECOND IMAGE INSTEAD OF REUSING THE TASK-ENV ONE. The task-env image contains
# tasks/banking_support/environment/tools.py, which encodes the identity-verification rule, the
# unlock-then-call protocol for specialist tools, the discoverable-tool catalogue and the escalation
# rules -- exactly the material this task withholds so the agent has to ELICIT it. A shell running in
# that container could `cat` the policy it is meant to have to ask about, so sharing the image would
# hand over the very thing the shell is confined to protect. This image is built so that material was
# never copied in: no task package, no transactional store, no admin token, no oracle, no evaluator code.
#
# STAGE 1 IS THROWN AWAY. The corpus is committed as {id,title,content} JSON, but `shell` exposes a
# FILESYSTEM, so the agent must see the same tree upstream's agents saw -- `.md` files whose names come
# from a specific id sanitizer, a `# {title}` body, and an INDEX.md the tool's own docstring tells the
# model to try. kb_export.py is the single definition of those bytes (verified byte-identical to
# upstream's exporter). Running it at BUILD time rather than at container start is what lets the final
# stage have a read-only root filesystem: nothing needs writing at boot, there is no per-run cost and no
# per-run failure mode, and the same image always yields the same tree.
#
# Build context is the repo root, like the task-env image:
#   docker build -f tasks/banking_support/environment/shell_sidecar/Dockerfile -t appsci-bench/banking-shell:latest .

# --- stage 1: materialize the shell-visible corpus (discarded) --------------------------------------
FROM python:3.11-slim AS export

WORKDIR /build
# Only what the exporter needs. Deliberately NOT the environment package: kb_export is a stdlib-only
# leaf module precisely so this stage cannot pull tools.py in behind it.
COPY tasks/banking_support/environment/_runtime/kb_export.py /build/kb_export.py
COPY tasks/banking_support/environment/documents             /build/documents/
RUN python /build/kb_export.py --src /build/documents --out /kb/knowledge_base \
    && test -f /kb/knowledge_base/INDEX.md

# --- stage 2: the runtime image ---------------------------------------------------------------------
# Debian-based on purpose. The agent gets GNU coreutils/grep/sed/awk, which is what upstream's runs had;
# busybox's near-equivalents differ in flags and output, and for this tool those differences ARE the
# observable behaviour. Verified present in this base: ls cat grep head tail find wc awk sed sort uniq
# cut tr xargs bash.
FROM python:3.11-slim

# The corpus, and the executor. Note what is NOT copied: the task package, the store, the setup scripts.
# Root-owned and world-readable, so the unprivileged runtime user can read but not modify it -- the
# read-only guarantee does not rest on the compose flag alone.
COPY --from=export --chown=root:root /kb /kb
COPY tasks/banking_support/environment/shell_sidecar/server.py /srv/server.py
RUN chmod -R a-w /kb /srv

# A stock unprivileged account (`nobody`). With the compose service's read-only root filesystem, dropped
# capabilities and no-new-privileges, a command has no writable path anywhere and nothing to escalate to.
USER 65534:65534

ENV BANKING_SHELL_KB_DIR=/kb/knowledge_base \
    BANKING_SHELL_PORT=8081 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8081
CMD ["python", "/srv/server.py"]

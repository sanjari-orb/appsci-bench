"""The shell sidecar's command executor -- the ONLY process in that container.

WHAT THIS IS. `shell` needs a real filesystem and real Unix utilities, because the agent drives it with
`ls`, `cat`, `grep`, `awk` and friends and every byte it sees comes from them. This serves that: it
accepts a command string over HTTP and runs it, with the exported knowledge corpus as the working
directory.

WHY IT LIVES IN A SEPARATE CONTAINER, which is the whole design. The task-env container holds material
this task deliberately withholds from the agent and expects it to ELICIT instead -- most of all
environment/tools.py, which encodes the identity-verification rule, the unlock-then-call protocol, the
discoverable-tool catalogue and the escalation rules. A shell running there could simply `cat` the
policy it is supposed to have to ask about. So the shell does not run there. This container is built
with the corpus and nothing else: no task source, no transactional store, no admin token, no oracle,
no evaluator code. The confinement is therefore not "access to secrets is restricted" but "the secrets
were never copied in", which is the stronger property and the one a reviewer can check by reading the
Dockerfile.

DELIBERATELY DUMB. Everything upstream-facing -- escape-pattern blocking, output sanitisation, the
grep-exit-1 shaping -- lives on the workspace side in shell_client.py, next to the rest of the tool's
behaviour. This process only executes and reports. Keeping the fidelity logic in one place means the
sidecar can be reasoned about purely as "does it confine", and it is Docker (see the compose service)
rather than this code that does the confining.

NOT A SECURITY BOUNDARY BY ITSELF. It runs whatever it is handed, on purpose. What makes that safe is
the container: unprivileged user, read-only root filesystem, all capabilities dropped, no new
privileges, an internal-only network, and a corpus that is public to the agent anyway. Never mount
anything else in here, and never add the task package to this image.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The working directory for every command, and the only content this container holds.
KB_DIR = pathlib.Path(os.environ.get("BANKING_SHELL_KB_DIR", "/kb/knowledge_base"))
# Shared secret with the workspace. Not the security boundary (the network is internal and the content
# is public) -- it is here so a misrouted request from anything else on the network is refused rather
# than silently executed.
TOKEN = os.environ.get("BANKING_SHELL_TOKEN", "")
PORT = int(os.environ.get("BANKING_SHELL_PORT", "8081"))
# Wall-clock ceiling per command. Mirrors upstream's default so a command that upstream would have
# killed is killed here too, and so one pathological command cannot stall a 97-case run.
TIMEOUT = int(os.environ.get("BANKING_SHELL_TIMEOUT", "30"))
# Output cap. A `cat *` over 698 documents is ~2.8MB, which would blow the agent's context and cost a
# fortune before it ever helped. Truncation is visible in the output so the agent can narrow its
# command rather than silently receiving a partial answer it believes is whole.
MAX_OUTPUT = int(os.environ.get("BANKING_SHELL_MAX_OUTPUT", "200000"))

_TRUNCATION_NOTE = "\n\n[output truncated at {n} characters]"


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + _TRUNCATION_NOTE.format(n=MAX_OUTPUT)


def run(command: str) -> dict:
    """Execute one command with the corpus as cwd. Never raises; every failure is a reported result.

    `shell=True` is the point, not an oversight: the agent is expected to use pipes, globs and
    redirection the way upstream's agents do, so the command has to reach a real shell. What bounds it
    is the container, not the invocation.
    """
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(KB_DIR),
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # 124 is what `timeout(1)` returns and what upstream reports, so the agent sees a familiar code.
        return {"exit_code": 124, "stdout": "",
                "stderr": f"Command timed out after {TIMEOUT} seconds"}
    except Exception as exc:  # noqa: BLE001 -- a broken command is a result, not a crash
        return {"exit_code": 1, "stdout": "", "stderr": f"Command failed: {exc}"}
    return {"exit_code": proc.returncode,
            "stdout": _truncate(proc.stdout or ""),
            "stderr": _truncate(proc.stderr or "")}


class Handler(BaseHTTPRequestHandler):
    server_version = "banking-shell/1.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's naming
        if self.path == "/health":
            self._send(200, {"ok": True, "kb_dir": str(KB_DIR), "exists": KB_DIR.is_dir()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            command = payload["command"]
            if not isinstance(command, str):
                raise TypeError("command must be a string")
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"error": f"bad request: {exc}"})
            return
        self._send(200, run(command))

    def log_message(self, fmt: str, *args) -> None:
        """Silence per-request logging: commands are agent-authored text and this container's stdout
        goes to the run log, so echoing them there would scatter agent input through the transcript."""
        return


def main() -> None:
    if not KB_DIR.is_dir():
        raise SystemExit(f"knowledge base directory missing: {KB_DIR}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

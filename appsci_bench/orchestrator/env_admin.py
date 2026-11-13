"""Evaluator-only transport to a task environment's private admin API."""
import json
import logging
import subprocess
import time

logger = logging.getLogger(__name__)

COUNTER_READ_ATTEMPTS = 3
COUNTER_READ_BUDGET_S = 60.0
COUNTER_READ_ATTEMPT_S = 20.0


class EnvAdminError(RuntimeError):
    """The admin transport failed, independently of the artifact's behavior."""


def make_env_admin_request(container: str, admin_token: str, *, port: int = 8080):
    base = f"http://localhost:{port}"
    # Runs INSIDE the env container. Reads the request body from stdin (never interpolated, so a 16 KB
    # initial_state and any quoting survive) and prints "<status>\n<body>". A 4xx/5xx is printed
    # rather than raised so the caller can tell "this env has no such route" (404) from a real failure.
    admin_script = (
        "import os, sys, json, urllib.request as u, urllib.error as e\n"
        "body = '' if os.environ['ADMIN_METHOD'] == 'GET' else sys.stdin.read()\n"
        "req = u.Request(os.environ['ADMIN_URL'], method=os.environ['ADMIN_METHOD'],\n"
        "                data=body.encode() if body else None)\n"
        "req.add_header('Authorization', 'Bearer ' + os.environ['ADMIN_TOKEN'])\n"
        "req.add_header('Content-Type', 'application/json')\n"
        "try:\n"
        "    resp = u.urlopen(req, timeout=float(os.environ.get('ADMIN_TIMEOUT', '30')))\n"
        "    print(resp.status)\n"
        "    sys.stdout.write(resp.read().decode())\n"
        "except e.HTTPError as err:\n"
        "    print(err.code)\n"
        "    sys.stdout.write(err.read().decode())\n"
    )

    def request(method, path, payload, timeout):
        try:
            result = subprocess.run(
                ["docker", "exec", *([] if method == "GET" else ["-i"]),
                 "-e", f"ADMIN_TOKEN={admin_token}",
                 "-e", f"ADMIN_URL={base}{path}",
                 "-e", f"ADMIN_METHOD={method}",
                 "-e", f"ADMIN_TIMEOUT={min(30.0, timeout)}",
                 container, "python", "-c", admin_script],
                capture_output=True, text=True, timeout=timeout,
                input=json.dumps(payload) if payload is not None else "",
            )
        except subprocess.TimeoutExpired:
            # TimeoutExpired embeds the entire command, including the admin token. Keep diagnostics
            # useful without copying credentials into transcripts, logs or a report.
            raise EnvAdminError(f"env admin {method} {path} on {container}: docker exec timed out "
                                f"after {timeout:g}s") from None
        except OSError as exc:
            raise EnvAdminError(f"env admin {method} {path} on {container}: {type(exc).__name__}") from None
        if result.returncode != 0:
            detail = result.stderr.strip().replace(admin_token, "[redacted]")
            raise EnvAdminError(f"env admin exec failed ({container}): {detail}")
        status, _, response = result.stdout.partition("\n")
        try:
            return int(status.strip()), response
        except ValueError:
            detail = result.stdout.replace(admin_token, "[redacted]")[:200]
            raise EnvAdminError(
                f"env admin call {method} {path} returned no status line: {detail!r}"
            ) from None

    def _admin(method: str, path: str, payload: dict | None = None) -> tuple[int, str]:
        """Retry only the read-only counter GET, within one 60-second transport budget.

        POSTs can already have applied when their response is lost. Replaying them would silently
        change the simulation budget or environment; the caller must recover the whole case instead.
        """
        if method != "GET" or path != "/admin/simulation_steps":
            return request(method, path, payload, 60)
        deadline = time.monotonic() + COUNTER_READ_BUDGET_S
        failure = None
        for attempt in range(COUNTER_READ_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                status, body = request(method, path, payload,
                                       min(COUNTER_READ_ATTEMPT_S, remaining))
                if status < 500:
                    return status, body
                failure = EnvAdminError(f"env admin {method} {path} on {container}: HTTP {status}")
            except EnvAdminError as exc:
                failure = exc
            if attempt + 1 < COUNTER_READ_ATTEMPTS and time.monotonic() < deadline:
                logger.warning("Counter read attempt %d failed; retrying: %s", attempt + 1, failure)
                time.sleep(min(0.25 * (attempt + 1), max(0, deadline - time.monotonic())))
        raise EnvAdminError(f"counter read retries exhausted: {failure}") from None

    return _admin

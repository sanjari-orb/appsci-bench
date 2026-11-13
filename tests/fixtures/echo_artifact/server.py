"""Creds-free echo artifact fixture for the deploy smoke (scripts/smoke_deploy_artifact.py).

Implements the minimal agentic-endpoint contract DockerArtifactDeployer validates and the evaluator
speaks:
    GET  /health   -> 200 {"status": "ok"}
    POST /respond  -> 200 {"response": <echo of the request's text>, "echo": <full body>}

Binds 0.0.0.0:$PORT (default 8080) inside the container (the deployer publishes it to a host port).
Pure stdlib: no
requirements.txt, no task-API creds, no network egress -- so the image builds fast and offline and the
smoke needs no secrets.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Bind the port the deployer publishes, read from $PORT (the deploy contract). Default 8080 keeps the
# fixture runnable standalone; the deploy smoke sets $PORT to a non-default to prove the injection.
PORT = int(os.environ.get("PORT", "8080"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/respond":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            data = {}
        # Echo whichever text-ish field the caller sent; the smoke only checks the round-trip.
        text = data.get("text") or data.get("message") or data.get("query") or ""
        self._send(200, {"response": text, "echo": data})

    def log_message(self, *args) -> None:  # silence per-request stderr noise
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

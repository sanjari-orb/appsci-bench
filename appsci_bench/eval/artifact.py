"""The artifact under evaluation: what the agent under test produces, and what the benchmark scores.

An artifact is anything the benchmark can QUERY through a standard interface (MCP / completions), or
a set of FILES the agent produced for the benchmark to read. For v1 the primary artifact is an E2E agentic
system; its internals (RAG, DBs, tools) are a black box. Produced files (deck/report/chart) are in
scope as query responses or file artifacts; static one-off deliverables with no queryable system are
out of scope (see docs).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class ArtifactQuery:
    intended_task: str
    customer_info_access: list = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    # OPTIONAL input files attached to THIS turn (eval.datatypes.File objects); empty for the
    # text-only tasks, which construct ArtifactQuery exactly as before. Typed as a bare `list` rather
    # than list[File] to keep this legacy module free of a `datatypes` import -- the transport only
    # reads file_type/file_name/file_data off each item. Files ride here rather than being flattened
    # into `intended_task` because the artifact needs the raw bytes (a .pptx template, a .csv), not a
    # prose rendering of them; collapsing them into text is what made the files-in contract a no-op.
    files: list = field(default_factory=list)


@dataclass
class ArtifactResponse:
    output: Any                          # query result: a dict, text, or produced-file pointer(s)
    messages: list[dict] = field(default_factory=list)
    trace: list | None = None            # optional artifact-self-reported internal LLM trace (debug)
    # OPTIONAL structured + produced-file channels of the session-API contract
    # (docs/artifact_agent_api.md marks text/json/files all optional). `output` stays the untouched
    # raw-dict escape hatch; these two are the PARSED contract channels, so callers never have to
    # re-parse the raw payload to find the deliverable. An artifact that returns only `choices` --
    # every text-only task today -- leaves both at their empty defaults.
    json: str | None = None              # JSON-encoded string (contract framing), not a dict
    files: list = field(default_factory=list)
    rejected_files: list = field(default_factory=list)   # {"file_type","file_name"} of refused files


def _files_to_wire(files: list) -> list[dict]:
    """Serialize File objects to the contract's wire shape (file_data base64-encoded).

    Delegates to each File's own `to_dict()` -- eval.datatypes.File already base64-encodes and already
    emits exactly file_type/file_name/file_data -- so the encoding lives in one place.
    """
    return [f.to_dict() for f in files]


def _parse_output_channels(result: dict) -> tuple[str | None, list, list]:
    """Pull the `json` + `files` contract channels off an artifact's response payload.

    Both are optional: an artifact returning only `choices` (every text-only task) yields
    `(None, [], [])`, which is exactly the pre-files-channel behaviour. A file whose `file_type` is
    outside ALLOWED_FILE_TYPES (after standard MIME types are mapped to short names) is skipped with
    a warning rather than raised -- one stray entry must not fail a turn whose real deliverable
    parsed fine. The skipped entry is returned as the third item ({"file_type","file_name"}) so the
    caller can record that a file was returned but rejected.
    """
    from .datatypes import EvalTypeError, File   # local import: mirrors artifact_client's convention

    raw_json = result.get("json")
    resp_json = raw_json if isinstance(raw_json, str) else None

    resp_files: list = []
    rejected: list = []
    for item in result.get("files") or []:
        try:
            resp_files.append(File.from_dict(item))
        except (EvalTypeError, KeyError, ValueError, TypeError, AttributeError) as e:
            logger.warning("artifact returned an unusable file; skipping: %s", e)
            if isinstance(item, dict):
                rejected.append({"file_type": item.get("file_type"), "file_name": item.get("file_name")})
    return resp_json, resp_files, rejected


@runtime_checkable
class Artifact(Protocol):
    def respond(self, query: ArtifactQuery) -> ArtifactResponse: ...


class InProcessArtifact:
    """Wraps a Python callable respond(query)->output|ArtifactResponse. For smokes / local stubs."""

    def __init__(self, fn: Callable[[ArtifactQuery], Any]):
        self._fn = fn

    def respond(self, query: ArtifactQuery) -> ArtifactResponse:
        out = self._fn(query)
        return out if isinstance(out, ArtifactResponse) else ArtifactResponse(output=out)


class HttpArtifact:
    """Query a handed-off artifact over HTTP using OpenAI completions format.

    The artifact endpoint should accept POST requests with the format:
        {"messages": [{"role": "user", "content": "..."}]}

    And return responses in the format:
        {"choices": [{"message": {"role": "assistant", "content": "..."}}]}

    Example usage:
        artifact = HttpArtifact(endpoint_url="http://appsci-order_to_cash:8080")
        artifact.wait_for_health()
        response = artifact.respond(ArtifactQuery(intended_task="check invoice 123"))

    Attributes:
        endpoint_url: URL of the artifact completions endpoint.
        timeout: Request timeout in seconds.
    """

    # 1200s, not 30s: an agentic artifact answers one /respond by running its own internal tool loop
    # (many sequential LLM calls), and a files-carrying request can be tens of MB. The real eval path
    # passes the orchestrator's ceiling explicitly (orchestrator/run.py); this default is what ad-hoc
    # callers and tests get, so it should not be the one value that times out early.
    #
    # RAISED 300 -> 1200 on 2026-09-18, in step with BenchmarkOrchestrator.QUERY_TIMEOUT_S and
    # generation.DEFAULT_QUERY_TIMEOUT_S. See the former for the measurement that forced it: at 300s
    # the slides eval was losing 53 cases across 17 trials, concentrated on the large-attachment
    # cases, and a lost case scores 0 rather than being excluded.
    DEFAULT_TIMEOUT = 1200
    HEALTH_TIMEOUT = 60
    # Ceiling on the serialized request body. Attached files are base64'd inline, so a large deck
    # inflates ~4/3x (slides_generator's biggest sample attaches ~21 MB -> ~28 MB encoded). Exceeding
    # this returns a named error rather than an opaque MemoryError or a server-side truncation.
    MAX_REQUEST_BYTES = 64 * 1024 * 1024

    def __init__(self, endpoint_url: str, timeout: int = DEFAULT_TIMEOUT):
        """Initialize the HttpArtifact.

        Args:
            endpoint_url: URL of the artifact completions endpoint.
            timeout: Request timeout in seconds.
        """
        self.endpoint_url = endpoint_url
        self.timeout = timeout

    def wait_for_health(self, timeout: int | None = None) -> None:
        """Wait for the artifact endpoint to be healthy.

        Polls the endpoint until it responds, or raises TimeoutError.

        Args:
            timeout: Maximum seconds to wait (defaults to HEALTH_TIMEOUT).

        Raises:
            TimeoutError: If endpoint doesn't respond within timeout.
        """
        timeout = timeout or self.HEALTH_TIMEOUT
        start_time = time.time()

        # Try /health first, fall back to base endpoint
        health_url = self.endpoint_url.rstrip("/")
        if not health_url.endswith("/health"):
            health_url = f"{health_url}/health"

        while time.time() - start_time < timeout:
            try:
                req = urllib.request.Request(health_url, method="GET")
                req.add_header("User-Agent", "appsci-bench-healthcheck")
                with urllib.request.urlopen(req, timeout=5) as response:
                    if response.status == 200:
                        logger.info(f"Artifact endpoint healthy: {self.endpoint_url}")
                        return
            except urllib.error.HTTPError as e:
                if e.code in (404, 405):
                    # No /health endpoint, try base URL
                    try:
                        req = urllib.request.Request(self.endpoint_url, method="GET")
                        with urllib.request.urlopen(req, timeout=5):
                            logger.info(f"Artifact endpoint reachable (no /health): {self.endpoint_url}")
                            return
                    except urllib.error.HTTPError as fallback_e:
                        # Any HTTP response means server is up
                        logger.debug(f"Artifact base URL returned HTTP {fallback_e.code}, treating as healthy")
                        return
                    except Exception as fallback_e:
                        logger.debug(f"Artifact base URL probe failed: {fallback_e}")
                else:
                    logger.debug(f"Health check returned HTTP {e.code}: {e.reason}")
            except urllib.error.URLError as e:
                logger.debug(f"Health check connection failed: {e.reason}")
            except Exception as e:
                logger.debug(f"Health check failed: {type(e).__name__}: {e}")

            time.sleep(1)

        raise TimeoutError(
            f"Artifact endpoint {self.endpoint_url} did not become healthy "
            f"within {timeout} seconds"
        )

    def respond(self, query: ArtifactQuery) -> ArtifactResponse:
        """Send a query to the artifact and return the response.

        Formats the query as an OpenAI completions request and parses the response.

        Files travel in a top-level `files` array alongside `messages` (the session-API `File` shape:
        file_type / file_name / file_data-as-base64), and produced files + structured output come back
        the same way. The key is OMITTED entirely when the query carries no files, so the request body
        for the text-only tasks is byte-identical to what it was before the files channel existed --
        that is what keeps this additive rather than a breaking change for already-built artifacts.

        Args:
            query: The query to send to the artifact.

        Returns:
            ArtifactResponse containing the artifact's output.
        """
        # Build messages list: history first, then current query
        messages = list(query.history)
        messages.append({"role": "user", "content": query.intended_task})

        # Create request. Only add "files" when there are some -- an always-present "files": [] would
        # change the wire bytes for every existing text-only artifact for no benefit.
        payload: dict[str, Any] = {"messages": messages}
        if query.files:
            payload["files"] = _files_to_wire(query.files)
        request_data = json.dumps(payload).encode("utf-8")

        # Guard the payload size explicitly: a produced/attached deck can be tens of MB once base64'd
        # (one slides_generator sample attaches a ~21 MB .pptx -> ~28 MB encoded). Failing loudly with
        # an attributable reason beats an opaque MemoryError or a silent server-side truncation.
        if len(request_data) > self.MAX_REQUEST_BYTES:
            detail = (f"request payload {len(request_data)} bytes exceeds MAX_REQUEST_BYTES "
                      f"{self.MAX_REQUEST_BYTES} ({len(query.files)} file(s) attached)")
            logger.error(detail)
            return ArtifactResponse(output={"error": detail})

        req = urllib.request.Request(
            self.endpoint_url,
            data=request_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            logger.debug(f"Querying artifact at {self.endpoint_url}")
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                resp_json, resp_files, rejected = _parse_output_channels(result)

                # Extract response from OpenAI completions format
                choices = result.get("choices", [])
                if choices:
                    message = choices[0].get("message", {})
                    content = message.get("content", "")
                    logger.debug(f"Artifact returned {len(content)} chars")
                    return ArtifactResponse(
                        output={"response": content},
                        messages=[{"role": "assistant", "content": content}],
                        trace=result.get("trace"),   # optional self-reported internal trace
                        json=resp_json,
                        files=resp_files,
                        rejected_files=rejected,
                    )

                # Handle non-standard response format
                if "response" in result:
                    logger.debug("Artifact returned non-standard 'response' format")
                    return ArtifactResponse(output=result, json=resp_json, files=resp_files,
                                        rejected_files=rejected)
                if "output" in result:
                    logger.debug("Artifact returned non-standard 'output' format")
                    return ArtifactResponse(output=result["output"], json=resp_json, files=resp_files,
                                        rejected_files=rejected)

                logger.warning(f"Artifact returned unexpected format: {list(result.keys())}")
                return ArtifactResponse(
                    output={"error": "Unexpected response format", "raw": result}
                )

        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass
            logger.error(f"Artifact query failed: HTTP {e.code} {e.reason}")
            logger.debug(f"Error response body: {error_body[:500] if error_body else '(empty)'}")
            return ArtifactResponse(
                output={"error": f"HTTP {e.code}: {e.reason}", "body": error_body}
            )
        except urllib.error.URLError as e:
            logger.error(f"Artifact connection failed: {e.reason}")
            return ArtifactResponse(
                output={"error": f"Connection failed: {e.reason}", "request_incomplete": True}
            )
        except json.JSONDecodeError as e:
            logger.error(f"Artifact returned invalid JSON: {e}")
            return ArtifactResponse(
                output={"error": f"Invalid JSON response: {e}"}
            )
        except Exception as e:
            logger.error(f"Artifact query failed: {type(e).__name__}: {e}")
            return ArtifactResponse(
                output={"error": f"Request failed: {e}", "request_incomplete": True}
            )



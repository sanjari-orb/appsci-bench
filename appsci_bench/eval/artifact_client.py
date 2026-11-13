"""Client for a served artifact agent over the session API.

The evaluator talks to the artifact through three calls (design: Artifact agent APIs):
  create_session() -> session_id
  query_session(session_id, ArtifactAgentQuery) -> ArtifactAgentOutput
  delete_session(session_id) -> bool

`ArtifactAgentClient` is the transport-agnostic Protocol the orchestration depends on.
`InProcessArtifactClient` is the in-process implementation used by tests / local stubs (and, later,
as the seam by which a build-phase in-process artifact is served). `HttpArtifactClient` is the real
transport used to score a deployed artifact over its `/respond` endpoint; `McpArtifactClient` is still
deferred (Docker env wiring) and raises NotImplementedError.

Framing: we enforce application/json -- a non-null `json` field must be valid JSON, else it is
dropped (warning logged) rather than failing the turn. Input and produced files ARE carried, in both directions: `ArtifactAgentQuery.files` reaches the artifact and
`ArtifactAgentOutput.files` comes back, base64-encoded inline per the contract
(docs/artifact_agent_api.md). Only `slides_generator` exercises this today; text-only tasks send and
receive exactly the bytes they did before the files channel existed.
"""
from __future__ import annotations

import itertools
import json as _json
import logging
from typing import Any, Callable, Protocol, runtime_checkable

from .datatypes import ArtifactAgentOutput, ArtifactAgentQuery, extract_json_object, materialize_files

logger = logging.getLogger(__name__)


class ArtifactClientError(RuntimeError):
    """A client-side problem talking to the artifact (bad session, malformed output)."""


class ArtifactQueryError(ArtifactClientError):
    """The artifact returned an error response (e.g. HttpArtifact's {"error": ...} on an
    unreachable/broken endpoint). Raised so the caller records the unit's `error` instead of
    silently accumulating empty turns against a dead artifact."""


class ArtifactTransportError(ArtifactQueryError):
    """The connection failed; server-side request completion is unknown."""


def _history_for_send(history: list[dict]) -> list[dict]:
    """Build the wire history for the next /respond from the stored session history: drop
    empty-content turns (providers such as Anthropic reject empty text blocks with a 400) and
    coalesce any consecutive same-role messages that dropping leaves adjacent (strict providers also
    reject non-alternating roles). The stored history keeps the TRUE content untouched -- we never
    persist a placeholder, so a genuinely-empty agent turn can't corrupt the model's view of the
    conversation on later turns."""
    out: list[dict] = []
    for m in history:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] = out[-1]["content"] + "\n" + m["content"]
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


class UnknownSessionError(ArtifactClientError):
    """query_session was called with a session id that is not open."""


@runtime_checkable
class ArtifactAgentClient(Protocol):
    """The session API the evaluator drives. Implementations may be in-process, MCP, or HTTP."""
    def create_session(self, context: dict | None = None) -> str: ...
    def query_session(self, session_id: str, query: ArtifactAgentQuery) -> ArtifactAgentOutput: ...
    def delete_session(self, session_id: str) -> bool: ...


def _coerce_output(out: Any) -> ArtifactAgentOutput:
    """Accept what a respond callable returned and normalize to an ArtifactAgentOutput.

    ArtifactAgentOutput -> as-is; dict -> from_dict; str -> text. A non-null `json` field that
    fails to parse is dropped with a warning rather than raised -- same policy as a malformed
    `files` entry (see artifact._parse_output_channels): one bad channel must not fail a turn
    whose other deliverables (e.g. a produced file) parsed fine.
    """
    if isinstance(out, ArtifactAgentOutput):
        result = out
    elif isinstance(out, dict):
        result = ArtifactAgentOutput.from_dict(out)
    elif isinstance(out, str):
        result = ArtifactAgentOutput(text=out)
    else:
        raise ArtifactClientError(
            f"artifact respond() must return ArtifactAgentOutput | dict | str, got {type(out).__name__}"
        )
    if result.json is not None:
        try:
            _json.loads(result.json)
        except (TypeError, ValueError) as e:
            logger.warning("artifact output 'json' is not valid JSON; dropping: %s", e)
            result.json = None
    return result


class InProcessArtifactClient:
    """Wraps a respond callable `fn(query) -> ArtifactAgentOutput | dict | str`, tracking sessions.

    No transport -- for tests/local stubs. Sessions are opaque ids; querying an unknown/closed
    session raises UnknownSessionError; delete is idempotent (returns False for unknown ids).
    """

    def __init__(self, fn: Callable[[ArtifactAgentQuery], Any], *, session_prefix: str = "sess"):
        self._fn = fn
        self._counter = itertools.count(1)
        self._open: set[str] = set()
        self._prefix = session_prefix

    def create_session(self, context: dict | None = None) -> str:
        # `context` is reserved (per-case starting state lands later); accepted and ignored for now.
        sid = f"{self._prefix}-{next(self._counter)}"
        self._open.add(sid)
        return sid

    def query_session(self, session_id: str, query: ArtifactAgentQuery) -> ArtifactAgentOutput:
        if session_id not in self._open:
            raise UnknownSessionError(f"session {session_id!r} is not open")
        return _coerce_output(self._fn(query))

    def delete_session(self, session_id: str) -> bool:
        if session_id in self._open:
            self._open.discard(session_id)
            return True
        return False  # unknown or already-deleted -- idempotent, no raise


class McpArtifactClient:
    """Query a served artifact over MCP. Deferred (Docker env wiring)."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def create_session(self, context: dict | None = None) -> str:
        raise NotImplementedError("MCP artifact transport not built yet (Docker env wiring is later)")

    def query_session(self, session_id: str, query: ArtifactAgentQuery) -> ArtifactAgentOutput:
        raise NotImplementedError("MCP artifact transport not built yet (Docker env wiring is later)")

    def delete_session(self, session_id: str) -> bool:
        raise NotImplementedError("MCP artifact transport not built yet (Docker env wiring is later)")


class HttpArtifactClient:
    """Query a served artifact over its HTTP `/respond` endpoint, tracking per-session history.

    The served artifact is stateless per request (it accepts `{"messages": [...]}` and returns an
    OpenAI-style completion), so this client owns the conversation: each session keeps the running
    message list and every `query_session` sends the full prior history plus the new user turn. This
    is what makes multi-turn eval coherent over a served artifact (the legacy in-process bridge in
    `eval_system._legacy_respond` drops history and is single-shot only).

    Reuses `appsci_bench.eval.artifact.HttpArtifact.respond`, which already formats the OpenAI request (history +
    current) and parses the completion.

    The completion carries ONE channel back -- the assistant message's content -- so this client also
    fills the structured half of the output from it: when the content IS a JSON object (the whole
    body, strictly -- see `datatypes.extract_json_object`), that object is put in
    `ArtifactAgentOutput.json` alongside the untouched `text`. A reply that isn't a bare object leaves
    `json` empty, and the task's own metrics report that as the contract failure it is; this client
    never guesses which part of a narrated reply was meant to be the answer.

    Additive for structured objectives, and a NO-OP for transcript judges: `text` is always the
    artifact's full reply, and because the two slots hold the same bytes whenever `json` is filled at
    all, the judge renderer drops the duplicate copy (`helpers/transcript._render_io`). So a judge's
    prompt is byte-identical to what it was before this slot was filled, and judge scores remain
    comparable across that change. Anything that renders a conversation for a model owes the same
    de-duplication -- filling `json` here means the payload is reachable twice, not that it changed.
    """

    def __init__(self, endpoint_url: str, *, timeout: float | None = None):
        # Local import: keep the eval package importable without the appsci_bench.eval.artifact dependency at
        # module load, mirroring eval_system._legacy_respond.
        from appsci_bench.eval.artifact import HttpArtifact

        self.endpoint_url = endpoint_url
        self._artifact = (
            HttpArtifact(endpoint_url=endpoint_url, timeout=timeout)
            if timeout is not None
            else HttpArtifact(endpoint_url=endpoint_url)
        )
        self._counter = itertools.count(1)
        # session_id -> running message history [{"role": "user"|"assistant", "content": str}, ...]
        self._sessions: dict[str, list[dict]] = {}

    def update_endpoint(self, endpoint_url: str) -> None:
        """Retarget this lane after its artifact was stopped and restarted."""
        self.endpoint_url = endpoint_url
        self._artifact.endpoint_url = endpoint_url

    def create_session(self, context: dict | None = None) -> str:
        # `context` is reserved (per-case starting state lands later); accepted and ignored for now.
        sid = f"http-{next(self._counter)}"
        self._sessions[sid] = []
        return sid

    def query_session(self, session_id: str, query: ArtifactAgentQuery) -> ArtifactAgentOutput:
        from appsci_bench.eval.artifact import ArtifactQuery, ArtifactResponse

        history = self._sessions.get(session_id)
        if history is None:
            raise UnknownSessionError(f"session {session_id!r} is not open")

        # Files must be bytes before they cross the wire. generation.py already materializes the
        # query, but doing it here too makes the client correct independent of its caller (the
        # conformance probe and the tests build queries directly). `File.materialize()` no-ops when
        # the bytes are already resident, so this is idempotent, not a re-read.
        materialize_files(query)

        user_text = query.text or query.json or ""
        resp = self._artifact.respond(
            ArtifactQuery(intended_task=user_text, history=_history_for_send(history),
                          files=list(query.files))
        )

        # Extract the assistant reply text from the ArtifactResponse.
        content = ""
        trace = None
        resp_json = None
        resp_files: list = []
        resp_rejected: list = []
        if isinstance(resp, ArtifactResponse):
            trace = resp.trace
            if isinstance(resp.output, dict) and resp.output.get("error"):
                # HttpArtifact.respond surfaces transport/JSON failures as {"error": ...} with empty
                # messages. Raise so the unit is marked errored instead of appending an empty turn and
                # burning the whole max_turns budget against a dead/unreachable artifact.
                error = f"artifact returned an error: {resp.output['error']}"
                # Keep the server's explanation in the transcript and scoring error. The exception
                # type and failure path stay the same; only the diagnostic text changes.
                body = resp.output.get("body")
                if isinstance(body, str) and body.strip():
                    error += f" -- {body.strip()}"
                error_type = (ArtifactTransportError if resp.output.get("request_incomplete")
                              else ArtifactQueryError)
                raise error_type(error)
            if resp.messages:
                content = resp.messages[-1].get("content", "") or ""
            elif isinstance(resp.output, dict):
                content = resp.output.get("response") or resp.output.get("text") or ""
            resp_json, resp_files = resp.json, list(resp.files)
            resp_rejected = list(resp.rejected_files)

        # Advance the session history with the TRUE content (empty allowed). Empty turns are dropped
        # only when building the wire history for the next call (see `_history_for_send`), never
        # persisted -- so a placeholder can't leak into the model's view of the conversation.
        #
        # Files are deliberately NOT replayed on later turns: the stored history is text-only, and an
        # attachment belongs to the turn that carried it. Re-sending a multi-MB deck on every turn of a
        # multi-turn case would multiply the upload for no gain, and `_history_for_send`'s empty-drop /
        # coalesce logic operates on strings. A multi-turn artifact is expected to retain its own
        # attachments across turns.
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": content})
        # Normalize through `_coerce_output`, the same way the in-process client does -- an artifact
        # emitting a malformed `json` field gets it dropped with a warning rather than losing the
        # whole turn (a good `files` deliverable alongside it must still score). `resp_json` is the
        # artifact's own explicit JSON channel when it used one; otherwise fall back to fishing a
        # bare JSON object out of the completions-protocol text content.
        return _coerce_output(
            ArtifactAgentOutput(
                text=content, json=resp_json or extract_json_object(content),
                files=resp_files, trace=trace, rejected_files=resp_rejected,
            )
        )

    def delete_session(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

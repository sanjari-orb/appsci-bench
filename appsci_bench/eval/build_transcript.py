"""The build-phase conversation, read out of a finished run and normalized for scoring.

The elicitation diagnostic scores what the agent got out of the build-time customer simulator, so it
needs that conversation as an ordered list of question/answer pairs. Two capture shapes exist and both
land in the same normalized form:

* a benchmark run's ``events-usersim.jsonl`` (the sim service emits one request/response pair per
  exchange), read by :func:`read_run_dir`;
* the human-baseline attempts, whose elicitation happened in a terminal chat and was captured as a raw
  pty log, read by :func:`read_transcript_file`. Scoring those with the same rubric is what gives the
  machine numbers a human reference point.

:func:`render_for_judge` turns either one into the text the judge sees. That text is the cached prefix
shared by every per-item judge call in a run, so it MUST be byte-identical for the same conversation
across items, processes and machines: a timestamp, a duration, a run id or an item index anywhere in it
drops every call to full price with no error to notice. Nothing volatile is rendered -- notably not
``BuildTranscript.source``, which carries a run id.

The three statuses are load-bearing and must stay distinguishable. ``empty`` means the agent never
asked anything: a real, informative zero that has happened in practice. ``unreadable`` means we do not
know what the agent asked, which is not a zero and must be reported as unmeasurable.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass

#: The sim service's own event log inside a run directory (one writer, append-only).
USERSIM_EVENTS_FILENAME = "events-usersim.jsonl"

#: Event types carrying the two halves of one exchange.
_REQUEST_EVENT = "usersim.request"
_RESPONSE_EVENT = "usersim.response"

#: Status values. `ok` = at least one exchange; `empty` = readable but the agent never asked;
#: `unreadable` = we cannot tell what was asked (missing file, corrupt log, unrecognized capture);
#: `no_stakeholder` = there was nobody to ask on this run (the no-user ablation arm).
#:
#: The last two are both "no score", but for opposite reasons, and the third is not a zero either.
#: `empty` is a real, informative zero: a stakeholder was there and the agent never used them.
#: `no_stakeholder` is not a finding about the agent at all -- no user-sim was started and the
#: requirements were handed over as files instead, so "missed 100% of the rubric" would be a false
#: statement about an agent that had nothing to miss.
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_UNREADABLE = "unreadable"
STATUS_NO_STAKEHOLDER = "no_stakeholder"

#: Line markers the human-baseline chat REPL writes for each side of the conversation.
_HUMAN_ASK_MARKER = "you > "
_HUMAN_ANSWER_MARKER = "sim > "

#: How a terminal renders an interrupt or end-of-file typed at the prompt. A prompt that ends this way
#: was abandoned, not asked -- the capture just happens to record it in the same shape as a question.
_ABANDONED_PROMPT_PREFIXES = ("^C", "^D")
#: The capture's own session boundary. Terminates an answer unconditionally -- see where it is used.
_SESSION_BANNER_PREFIX = "## Session "

#: Terminal control noise in a pty capture: CSI/OSC escape sequences, then bare control characters.
#: Both real ESC bytes and the CARET NOTATION some captures record instead ("^[[A" as four ordinary
#: characters) -- the committed human-baseline transcripts use the latter, so matching only the byte
#: form left arrow-key presses in the text and turned them into phantom questions. Deliberately does
#: NOT match bare caret controls like "^C": _ABANDONED_PROMPT_PREFIXES matches those after cleaning.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]"
    r"|\^\[\[[0-9;?]*[ -/]*[@-~]|\^\[[@-Z\\-_]"
)
_CONTROL_RE = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Exchange:
    """One round trip: what the agent asked, and what the stakeholder said back."""
    turn: int
    question: str
    answer: str


@dataclass(frozen=True)
class BuildTranscript:
    """A whole build conversation, normalized. `source` is for the report, never for the judge."""
    exchanges: tuple[Exchange, ...]
    source: str
    status: str

    def __len__(self) -> int:
        return len(self.exchanges)


def _unreadable(source: str) -> BuildTranscript:
    return BuildTranscript(exchanges=(), source=source, status=STATUS_UNREADABLE)


def no_stakeholder_transcript() -> BuildTranscript:
    """The transcript of a run that had no build-time stakeholder (the no-user ablation arm).

    Empty, like an unread one, but carrying a status that says WHY -- which is the whole point. An
    orchestrator running that arm passes this instead of a run directory, and the elicitation
    diagnostic reports "not applicable" rather than a zero it would otherwise be entitled to infer.
    """
    return BuildTranscript(exchanges=(), source="no user-sim ran (no-user arm)",
                           status=STATUS_NO_STAKEHOLDER)


def _finish(exchanges: list[Exchange], source: str) -> BuildTranscript:
    return BuildTranscript(
        exchanges=tuple(exchanges),
        source=source,
        status=STATUS_OK if exchanges else STATUS_EMPTY,
    )


# --- run directories ---------------------------------------------------------------------------
def read_run_dir(run_dir: str | pathlib.Path) -> BuildTranscript:
    """Read the build conversation out of a finished run's user-sim event log.

    Pairs the request and response events by their `turn` field, never by file order: the same log
    interleaves the sim's own `model.request` / `model.response` lines between the halves of an
    exchange, and a slow write could reorder anything. A request with no matching response (the run
    died mid-exchange) is kept with an empty answer -- the agent did ask, and that is what the metric
    scores.
    """
    path = pathlib.Path(run_dir) / USERSIM_EVENTS_FILENAME
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _unreadable(str(path))

    questions: dict[int, str] = {}
    answers: dict[int, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # A corrupt line means we cannot trust what the agent asked; do not score a partial
            # conversation as if it were whole.
            return _unreadable(str(path))
        if not isinstance(event, dict):
            return _unreadable(str(path))

        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        turn = payload.get("turn")
        if not isinstance(turn, int) or isinstance(turn, bool):
            continue

        if event.get("event_type") == _REQUEST_EVENT:
            questions[turn] = str(payload.get("question") or "")
        elif event.get("event_type") == _RESPONSE_EVENT:
            answers[turn] = str(payload.get("answer") or "")

    exchanges = [
        Exchange(turn=turn, question=questions[turn], answer=answers.get(turn, ""))
        for turn in sorted(questions)
    ]
    return _finish(exchanges, str(path))


# --- plain transcripts -------------------------------------------------------------------------
def _clean_capture_line(line: str) -> str:
    """Strip terminal noise from one pty-captured line and apply its backspace erasures."""
    text = _ANSI_RE.sub("", line.rstrip("\r\n"))
    out: list[str] = []
    for char in text:
        if char == "\b":
            if out:
                out.pop()
        else:
            out.append(char)
    return _CONTROL_RE.sub("", "".join(out)).strip()


def read_transcript_file(path: str | pathlib.Path) -> BuildTranscript:
    """Read a human-baseline attempt's captured chat log.

    The capture is a raw terminal log: it opens with the persona's whole system prompt, then the REPL
    exchange. Only the two REPL markers are collected, which is what keeps the persona's own knowledge
    out of the judge's view -- the judge is shown what the agent was told, never what the stakeholder
    knew. An answer runs on until the next marker, so multi-line answers survive whole. Consecutive
    questions with nothing between them (a mistyped line, retyped) are kept with an empty answer -- the
    human did type them. A prompt that was abandoned rather than submitted is dropped.

    A directory is accepted as a convenience and resolved to its ``logs/transcript.md``. A file that
    carries neither marker is `unreadable` rather than guessed at: better no number than a number
    parsed out of prose.
    """
    target = pathlib.Path(path)
    if target.is_dir():
        target = target / "logs" / "transcript.md"
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _unreadable(str(target))

    pending: str | None = None       # question whose answer is still being collected
    answer_lines: list[str] = []
    collecting_answer = False
    in_fence = False                 # inside a fenced block WITHIN an answer, not the log's own wrapping
    exchanges: list[Exchange] = []
    saw_marker = False

    def flush() -> None:
        nonlocal pending, answer_lines, collecting_answer, in_fence
        while answer_lines and (not answer_lines[-1].strip()
                                or answer_lines[-1].lstrip().startswith("```")):
            answer_lines.pop()       # a block left open at a boundary trails a fence that is not content
        if pending is not None:
            exchanges.append(Exchange(
                turn=len(exchanges) + 1,
                question=pending,
                answer="\n".join(answer_lines).strip(),
            ))
        pending, answer_lines, collecting_answer, in_fence = None, [], False, False

    for line in raw.splitlines():
        stripped = line.rstrip("\r\n")
        if stripped.startswith(_HUMAN_ASK_MARKER):
            saw_marker = True
            flush()
            asked = _clean_capture_line(stripped[len(_HUMAN_ASK_MARKER):])
            # An empty or interrupted prompt is where the session ended, not a question the human
            # asked. Recording it would put a bare traceback in front of the judge.
            if asked and not asked.startswith(_ABANDONED_PROMPT_PREFIXES):
                pending = asked
            continue
        if stripped.startswith(_HUMAN_ANSWER_MARKER):
            saw_marker = True
            collecting_answer = True
            in_fence = False
            answer_lines = [_clean_capture_line(stripped[len(_HUMAN_ANSWER_MARKER):])]
            continue
        if collecting_answer:
            # Continuation of a multi-line answer. A session banner or the log's own wrapping fence ends
            # it; everything before the first question (the dumped system prompt) is never reached.
            #
            # Fences are tracked as a PAIR rather than treated as terminators outright, because a
            # stakeholder can legitimately answer with one: on a task whose whole subject is a response
            # contract, quoting the JSON shape is the natural reply. Treating that opening fence as the
            # end of the answer truncated it silently, and the item then scored "asked, not elicited" on
            # evidence the stakeholder had actually given. So a fence opens a block when the answer
            # already has text, closes it when one is open, and only a fence or heading met OUTSIDE a
            # block ends the answer. A marker line always wins over fence state, so an unclosed block
            # cannot swallow the rest of the file.
            if stripped.lstrip().startswith("```"):
                if in_fence:
                    in_fence = False
                    answer_lines.append(_clean_capture_line(stripped))
                elif answer_lines and any(x.strip() for x in answer_lines):
                    in_fence = True
                    answer_lines.append(_clean_capture_line(stripped))
                else:
                    flush()      # a fence before any answer text is the log's own wrapping
                continue
            # A session banner ends the answer whatever the fence state. It is the one boundary that must
            # never be crossed -- everything after it belongs to another session, including a dumped
            # persona prompt -- so it does not get to depend on fences being balanced. Any OTHER heading
            # is ordinary content inside a quoted block and only terminates outside one.
            if stripped.startswith(_SESSION_BANNER_PREFIX):
                flush()
                continue
            if stripped.startswith("## ") and not in_fence:
                flush()
                continue
            answer_lines.append(_clean_capture_line(stripped))
    flush()

    if not saw_marker:
        return _unreadable(str(target))
    return _finish(exchanges, str(target))


# --- the judge's view --------------------------------------------------------------------------
_JUDGE_HEADER = "BUILD CONVERSATION (the agent's interview of the customer stakeholder)"
_NO_ANSWER = "(no answer was recorded for this question)"

#: The structural markers below are the only thing separating the agent's words from the stakeholder's
#: in the judge's view, and the agent chooses its own question text -- so it could otherwise post a
#: question containing "STAKEHOLDER ANSWERED:" and have a fabricated answer scored as real. Each marker
#: is destroyed inside quoted text, so a forged one cannot be mistaken for one this renderer wrote.
#: A no-op on every real conversation, which is why the cached prefix is unchanged for existing runs.
_MARKER_DEFANGED = {
    "AGENT ASKED:": "[agent asked:]",
    "STAKEHOLDER ANSWERED:": "[stakeholder answered:]",
    "--- exchange ": "[exchange ]",
}


def _defang_markers(text: str) -> str:
    """Strip this renderer's own structural markers out of text the agent or simulator controls."""
    for marker, replacement in _MARKER_DEFANGED.items():
        text = text.replace(marker, replacement)
    return text


def render_for_judge(transcript: BuildTranscript) -> str:
    """Render the conversation as the judge's cached prefix.

    Byte-identical for the same conversation, always. Nothing here may depend on the run, the item
    being judged, the clock, or the machine -- see the module docstring for why that is a correctness
    requirement and not a nicety.
    """
    if not transcript.exchanges:
        return f"{_JUDGE_HEADER}\n\n(The agent never contacted the stakeholder.)\n"

    blocks = [_JUDGE_HEADER, ""]
    for index, exchange in enumerate(transcript.exchanges, start=1):
        blocks.append(f"--- exchange {index} ---")
        blocks.append("AGENT ASKED:")
        blocks.append(_defang_markers(exchange.question.strip()) or "(empty)")
        blocks.append("")
        blocks.append("STAKEHOLDER ANSWERED:")
        blocks.append(_defang_markers(exchange.answer.strip()) or _NO_ANSWER)
        blocks.append("")
    return "\n".join(blocks)

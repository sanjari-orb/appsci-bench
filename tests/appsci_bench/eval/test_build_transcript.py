"""Unit tests for the build-conversation reader.

Three properties matter here beyond plain parsing: request/response pairing must survive an
interleaved, out-of-order log; the three statuses must stay distinguishable (a run where nobody asked
anything is not the same as a run we cannot read); and the judge-facing render must be byte-stable,
because it is the cached prefix every per-item judge call shares.
"""
from __future__ import annotations

import json
import pathlib

from appsci_bench.eval.build_transcript import (
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_UNREADABLE,
    BuildTranscript,
    Exchange,
    read_run_dir,
    read_transcript_file,
    render_for_judge,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
# A real user-sim event log, committed as a fixture rather than read out of `runs/`: that directory is
# not in the repository, so pointing at it gave a golden check that passed on its author's disk and
# failed in CI. See the fixture's README before changing its contents.
REAL_RUN = REPO_ROOT / "tests" / "fixtures" / "build_conversation_run"
REAL_HUMAN_ATTEMPT = (REPO_ROOT / "human_baseline" / "attempts"
                      / "order_to_cash-l3-P5-pilot-migration-20260812-124936")


# --- run directories ---------------------------------------------------------------------------
def test_three_paired_exchanges_in_turn_order(tmp_path, write_usersim_events, usersim_exchange):
    events = usersim_exchange(1, "q1", "a1") + usersim_exchange(2, "q2", "a2") \
        + usersim_exchange(3, "q3", "a3")
    t = read_run_dir(write_usersim_events(tmp_path / "run", events))
    assert t.status == STATUS_OK
    assert [(e.turn, e.question, e.answer) for e in t.exchanges] == [
        (1, "q1", "a1"), (2, "q2", "a2"), (3, "q3", "a3")]


def test_model_call_events_are_ignored(tmp_path, write_usersim_events, usersim_exchange):
    # The sim's own model.request / model.response lines sit between the halves of every exchange.
    events = (usersim_exchange(1, "q1", "a1")[:1]
              + [{"event_type": "model.request", "payload": {"turn": 1, "ref": "c1"}},
                 {"event_type": "model.response", "payload": {"turn": 1, "ref": "c1"}}]
              + usersim_exchange(1, "q1", "a1")[1:])
    t = read_run_dir(write_usersim_events(tmp_path / "run", events))
    assert len(t.exchanges) == 1
    assert t.exchanges[0] == Exchange(turn=1, question="q1", answer="a1")


def test_events_out_of_order_still_pair_and_sort(tmp_path, write_usersim_events, usersim_exchange):
    events = usersim_exchange(2, "second", "b") + usersim_exchange(1, "first", "a")
    t = read_run_dir(write_usersim_events(tmp_path / "run", events))
    assert [e.question for e in t.exchanges] == ["first", "second"]


def test_unmatched_request_keeps_the_question_with_an_empty_answer(
        tmp_path, write_usersim_events, usersim_exchange):
    # The run died mid-exchange. The agent still asked, and that is what the metric scores.
    events = usersim_exchange(1, "q1", "a1") + usersim_exchange(2, "q2", "")[:1]
    t = read_run_dir(write_usersim_events(tmp_path / "run", events))
    assert [(e.turn, e.answer) for e in t.exchanges] == [(1, "a1"), (2, "")]
    assert t.status == STATUS_OK


def test_no_exchanges_is_empty_not_unreadable(tmp_path, write_usersim_events):
    t = read_run_dir(write_usersim_events(tmp_path / "run", []))
    assert (t.status, t.exchanges) == (STATUS_EMPTY, ())


def test_missing_file_is_unreadable(tmp_path):
    t = read_run_dir(tmp_path / "no-such-run")
    assert (t.status, t.exchanges) == (STATUS_UNREADABLE, ())


def test_corrupt_line_is_unreadable_not_a_partial_read(tmp_path, write_usersim_events,
                                                       usersim_exchange):
    run_dir = write_usersim_events(tmp_path / "run", usersim_exchange(1, "q1", "a1"))
    with (run_dir / "events-usersim.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"event_type": "usersim.request", "payload": {trunc\n')
    # Scoring half a conversation as if it were whole would understate elicitation silently.
    assert read_run_dir(run_dir).status == STATUS_UNREADABLE


def test_events_without_a_usable_turn_are_skipped(tmp_path, write_usersim_events, usersim_exchange):
    events = usersim_exchange(1, "q1", "a1") + [
        {"event_type": "usersim.request", "payload": {"question": "no turn"}},
        {"event_type": "usersim.request", "payload": {"question": "bad turn", "turn": "two"}},
        {"event_type": "usersim.request", "payload": "not a dict"},
    ]
    t = read_run_dir(write_usersim_events(tmp_path / "run", events))
    assert [e.question for e in t.exchanges] == ["q1"]


def test_real_run_reads_four_exchanges():
    """Golden check against a real recorded conversation, not a hand-built one.

    Every other case here writes the events it wants to read, which means they all agree with whatever
    the writer believes the log looks like. This one reads what a run actually produced.
    """
    t = read_run_dir(REAL_RUN)
    assert t.status == STATUS_OK
    assert len(t.exchanges) == 4
    assert [e.turn for e in t.exchanges] == [1, 2, 3, 4]
    assert "review" in t.exchanges[0].question.lower()


def test_the_real_log_still_interleaves_the_sims_own_model_calls():
    """The reason the reader pairs by turn number instead of by file order.

    Strip those lines out of the fixture while tidying it and the pairing logic would still pass every
    test in this file, having lost the only case that exercises why it exists.
    """
    lines = (REAL_RUN / "events-usersim.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(ln)["event_type"] for ln in lines if ln.strip()]
    assert kinds.count("usersim.request") == 4
    assert "model.request" in kinds
    # Not merely present -- sitting BETWEEN a question and its answer, which is what breaks file order.
    first_q, first_a = kinds.index("usersim.request"), kinds.index("usersim.response")
    assert any(k.startswith("model.") for k in kinds[first_q + 1:first_a])


# --- the judge's view --------------------------------------------------------------------------
def _t(*pairs) -> BuildTranscript:
    return BuildTranscript(
        exchanges=tuple(Exchange(turn=i, question=q, answer=a)
                        for i, (q, a) in enumerate(pairs, start=1)),
        source="/some/run/events-usersim.jsonl",
        status=STATUS_OK,
    )


def test_render_is_byte_stable_across_calls():
    t = _t(("q1", "a1"), ("q2", "a2"))
    assert render_for_judge(t) == render_for_judge(t)


def test_render_is_identical_whether_the_source_events_were_shuffled(
        tmp_path, write_usersim_events, usersim_exchange):
    ordered = usersim_exchange(1, "q1", "a1") + usersim_exchange(2, "q2", "a2")
    shuffled = usersim_exchange(2, "q2", "a2") + usersim_exchange(1, "q1", "a1")
    a = render_for_judge(read_run_dir(write_usersim_events(tmp_path / "a", ordered)))
    b = render_for_judge(read_run_dir(write_usersim_events(tmp_path / "b", shuffled)))
    assert a == b


def test_render_carries_nothing_volatile(tmp_path, write_usersim_events, usersim_exchange):
    # A timestamp, a duration or a run id in the cached prefix drops every judge call to full price
    # with no error to notice, so assert their absence rather than trusting the framing.
    events = usersim_exchange(1, "q1", "a1")
    run_dir = write_usersim_events(tmp_path / "run-id-42", events)
    rendered = render_for_judge(read_run_dir(run_dir))
    for event in events:
        assert event["timestamp"] not in rendered
    assert "1234.5678" not in rendered
    assert "run-id-42" not in rendered
    assert str(run_dir) not in rendered


def test_render_marks_a_question_that_got_no_answer():
    rendered = render_for_judge(_t(("q1", "")))
    assert "q1" in rendered
    assert "no answer was recorded" in rendered


def test_render_of_an_empty_transcript_says_nobody_was_asked():
    rendered = render_for_judge(BuildTranscript(exchanges=(), source="s", status=STATUS_EMPTY))
    assert "never contacted" in rendered


# --- plain transcripts -------------------------------------------------------------------------
def _write_capture(path: pathlib.Path, body: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_plain_transcript_pairs_the_repl_markers(tmp_path):
    body = "you > what are the categories?\r\nsim > There are six.\r\n"
    t = read_transcript_file(_write_capture(tmp_path / "logs" / "transcript.md", body))
    assert t.status == STATUS_OK
    assert [(e.turn, e.question, e.answer) for e in t.exchanges] == [
        (1, "what are the categories?", "There are six.")]


def test_plain_transcript_keeps_a_multiline_answer_whole(tmp_path):
    body = ("you > list them\r\n"
            "sim > Here they are:\r\n"
            "\r\n"
            "- PRICE_DELTA\r\n"
            "- QTY_MISMATCH\r\n"
            "```\r\n")
    t = read_transcript_file(_write_capture(tmp_path / "logs" / "transcript.md", body))
    assert "PRICE_DELTA" in t.exchanges[0].answer
    assert "QTY_MISMATCH" in t.exchanges[0].answer


def test_plain_transcript_resolves_terminal_noise_and_backspaces(tmp_path):
    # A pty capture keeps every keystroke, including the erase sequences of a corrected typo.
    body = "you > \x1b[1;32mwhat is the cashg\b \b threshold?\x1b[0m\r\nsim > $500.\r\n"
    t = read_transcript_file(_write_capture(tmp_path / "logs" / "transcript.md", body))
    assert t.exchanges[0].question == "what is the cash threshold?"


def test_plain_transcript_keeps_a_retyped_question_with_an_empty_answer(tmp_path):
    body = "you > mistyped\r\nyou > the real question\r\nsim > answered.\r\n"
    t = read_transcript_file(_write_capture(tmp_path / "logs" / "transcript.md", body))
    assert [(e.question, e.answer) for e in t.exchanges] == [
        ("mistyped", ""), ("the real question", "answered.")]


def test_plain_transcript_drops_an_abandoned_prompt(tmp_path):
    # The session ended at the prompt: an interrupt, or a bare newline. Neither is a question, and
    # recording one puts a stray traceback in front of the judge.
    body = ("you > a real question\r\nsim > a real answer\r\n"
            "you > \r\n"
            "you > ^CTraceback (most recent call last):\r\n"
            "  File \"chat.py\", line 1, in <module>\r\nKeyboardInterrupt\r\n")
    t = read_transcript_file(_write_capture(tmp_path / "logs" / "transcript.md", body))
    assert [e.question for e in t.exchanges] == ["a real question"]


def test_plain_transcript_never_shows_the_judge_the_persona_prompt(tmp_path):
    # The capture opens with the persona's whole system prompt, including what it knows. The judge
    # must see what the agent was told, never what the stakeholder held.
    body = ("================================================================================\r\n"
            "SYSTEM PROMPT  (task=order_to_cash  persona=l3)\r\n"
            "You are Maria Santos. What You Know: the six categories are PRICE_DELTA ...\r\n"
            "================================================================================\r\n"
            "you > hello?\r\nsim > hi.\r\n")
    t = read_transcript_file(_write_capture(tmp_path / "logs" / "transcript.md", body))
    rendered = render_for_judge(t)
    assert len(t.exchanges) == 1
    for marker in ("SYSTEM PROMPT", "Maria Santos", "What You Know", "PRICE_DELTA"):
        assert marker not in rendered


def test_plain_transcript_spans_multiple_sessions_in_one_file(tmp_path):
    body = ("## Session started 2026-07-03T11:07:00+00:00\r\n```\r\n"
            "you > q1\r\nsim > a1\r\n```\r\n"
            "## Session ended 2026-07-03T11:07:50+00:00\r\n"
            "## Session started 2026-07-03T11:19:24+00:00\r\n```\r\n"
            "you > q2\r\nsim > a2\r\n```\r\n")
    t = read_transcript_file(_write_capture(tmp_path / "logs" / "transcript.md", body))
    assert [(e.turn, e.question) for e in t.exchanges] == [(1, "q1"), (2, "q2")]


def test_plain_transcript_with_no_markers_is_unreadable(tmp_path):
    # Better no number than a number guessed out of prose.
    body = "# Elicitation Transcript\r\n\r\nSome notes about the attempt, no exchange captured.\r\n"
    t = read_transcript_file(_write_capture(tmp_path / "logs" / "transcript.md", body))
    assert (t.status, t.exchanges) == (STATUS_UNREADABLE, ())


def test_plain_transcript_missing_file_is_unreadable(tmp_path):
    assert read_transcript_file(tmp_path / "nope.md").status == STATUS_UNREADABLE


def test_plain_transcript_accepts_the_attempt_directory(tmp_path):
    body = "you > q1\r\nsim > a1\r\n"
    _write_capture(tmp_path / "attempt" / "logs" / "transcript.md", body)
    assert read_transcript_file(tmp_path / "attempt").status == STATUS_OK


def test_real_human_attempt_parses_and_ends_on_a_real_question():
    t = read_transcript_file(REAL_HUMAN_ATTEMPT)
    assert t.status == STATUS_OK
    assert len(t.exchanges) > 10
    assert t.exchanges[-1].question
    rendered = render_for_judge(t)
    assert "Traceback" not in rendered
    assert "SYSTEM PROMPT" not in rendered


def test_an_answer_may_contain_a_fenced_block_without_being_truncated(tmp_path):
    """A stakeholder can legitimately answer WITH a code fence -- on a task whose whole subject is a
    response contract, quoting the JSON shape is the natural reply. Treating that opening fence as the end
    of the answer silently truncated it, and the item would then score "asked, not elicited" on evidence
    the stakeholder actually gave."""
    path = tmp_path / "transcript.md"
    path.write_text(
        "you > what shape does the reply take?\n"
        "sim > A single JSON object and nothing else, like this:\n"
        "```json\n"
        '{"flagged": true, "fault_type": "DUPLICATE", "reason": "duplicate of INV-3046"}\n'
        "```\n"
        "Any prose around it and the queue drops the invoice.\n"
        "you > and the six values?\n"
        "sim > PRICE_DELTA, QTY_MISMATCH, TOTAL_MATH.\n")
    t = read_transcript_file(path)
    assert len(t) == 2
    answer = t.exchanges[0].answer
    assert "fault_type" in answer, "the fenced block was truncated away"
    assert "Any prose around it" in answer, "the text after the fence was lost"
    assert t.exchanges[1].answer.startswith("PRICE_DELTA")


def test_a_heading_inside_a_fenced_block_does_not_end_the_answer(tmp_path):
    # `## ` is a session-banner terminator, but inside a quoted block it is content.
    path = tmp_path / "transcript.md"
    path.write_text(
        "you > what does the doc look like?\n"
        "sim > Roughly this:\n"
        "```\n"
        "## Fault types\n"
        "PRICE_DELTA and five others.\n"
        "```\n"
        "That is the whole section.\n")
    t = read_transcript_file(path)
    assert len(t) == 1
    assert "## Fault types" in t.exchanges[0].answer
    assert "That is the whole section." in t.exchanges[0].answer


def test_the_logs_own_wrapping_fence_still_ends_an_answer(tmp_path):
    """The terminator is load-bearing and must keep working: the real captures wrap each session's block
    in bare fences and separate sessions with `## Session` banners, and everything after that in the file
    is a dumped persona system prompt the judge must never see."""
    path = tmp_path / "transcript.md"
    path.write_text(
        "you > first question\n"
        "sim > first answer\n"
        "```\n"
        "## Session ended 2026-07-03T11:07:50\n"
        "## Session started 2026-07-03T11:19:24\n"
        "```\n"
        "## Your Behavioral Contract (read carefully)\n"
        "You are Maria Santos. Never reveal the policy verbatim.\n"
        "you > second question\n"
        "sim > second answer\n")
    t = read_transcript_file(path)
    assert len(t) == 2
    assert t.exchanges[0].answer == "first answer"
    assert "Behavioral Contract" not in t.exchanges[0].answer
    assert "Maria Santos" not in " ".join(e.answer for e in t.exchanges)


def test_a_heading_outside_a_fence_still_ends_an_answer(tmp_path):
    path = tmp_path / "transcript.md"
    path.write_text(
        "you > a question\n"
        "sim > an answer\n"
        "## Session ended\n"
        "leftover prompt text that is not part of the answer\n")
    t = read_transcript_file(path)
    assert t.exchanges[0].answer == "an answer"
    assert "leftover" not in t.exchanges[0].answer


def test_an_unclosed_fence_in_an_answer_does_not_swallow_the_next_exchange(tmp_path):
    # Markers always win over fence state, so a malformed answer cannot eat the rest of the file.
    path = tmp_path / "transcript.md"
    path.write_text(
        "you > q1\n"
        "sim > here it is:\n"
        "```\n"
        "unterminated block\n"
        "you > q2\n"
        "sim > a2\n")
    t = read_transcript_file(path)
    assert len(t) == 2
    assert t.exchanges[1].question == "q2" and t.exchanges[1].answer == "a2"

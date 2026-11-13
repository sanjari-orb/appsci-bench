"""Unit tests: HttpArtifact's `/respond` wire format (files + json channels, both directions).

No network -- `urllib.request.urlopen` is patched at this module's import site (the same pattern
test_deployer.py uses), so we can assert on the exact bytes that would go out and on how a canned
response body is parsed back.

The load-bearing test here is `test_text_only_request_is_byte_identical_to_legacy`: only
slides_generator attaches files, so every other task must keep producing exactly the request body it
produced before the files channel existed.
"""
from __future__ import annotations

import base64
import json
from unittest import mock

import pytest

from appsci_bench.eval.artifact import ArtifactQuery, HttpArtifact
from appsci_bench.eval.datatypes import File


def _resp(body: dict):
    """A urlopen stand-in returning `body` as JSON, with the context-manager protocol stubbed."""
    r = mock.MagicMock()
    r.read.return_value = json.dumps(body).encode("utf-8")
    r.__enter__ = mock.Mock(return_value=r)
    r.__exit__ = mock.Mock(return_value=False)
    return r


def _roundtrip(query: ArtifactQuery, body: dict | None = None):
    """Drive the real respond() against a faked urlopen; return (sent_payload_dict, response)."""
    body = body if body is not None else {"choices": [{"message": {"content": "ok"}}]}
    art = HttpArtifact(endpoint_url="http://x/respond")
    with mock.patch("appsci_bench.eval.artifact.urllib.request.urlopen",
                    return_value=_resp(body)) as urlopen:
        resp = art.respond(query)
    sent = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
    return sent, resp


# --- request side ------------------------------------------------------------------------------

def test_text_only_request_is_byte_identical_to_legacy():
    # THE backward-compatibility guard: no `files` key at all when nothing is attached, and `content`
    # stays a plain string. Any change here breaks all 9 text-only tasks.
    sent, _ = _roundtrip(ArtifactQuery(intended_task="hi",
                                       history=[{"role": "user", "content": "prior"}]))
    assert sent == {"messages": [{"role": "user", "content": "prior"},
                                 {"role": "user", "content": "hi"}]}
    assert "files" not in sent                       # not even an empty array


def test_request_with_files_emits_top_level_files_array():
    files = [File(file_type="csv", file_name="m.csv", file_data=b"a,b\n1,2"),
             File(file_type="pptx", file_name="t.pptx", file_data=b"\x50\x4b\x03\x04binary")]
    sent, _ = _roundtrip(ArtifactQuery(intended_task="build", files=files))
    assert sent["messages"] == [{"role": "user", "content": "build"}]   # content still a string
    assert [f["file_name"] for f in sent["files"]] == ["m.csv", "t.pptx"]
    assert [f["file_type"] for f in sent["files"]] == ["csv", "pptx"]
    # base64 must round-trip to the original bytes, including non-UTF8 binary
    assert base64.b64decode(sent["files"][0]["file_data"]) == b"a,b\n1,2"
    assert base64.b64decode(sent["files"][1]["file_data"]) == b"\x50\x4b\x03\x04binary"


def test_request_exceeding_max_bytes_returns_named_error_without_sending():
    art = HttpArtifact(endpoint_url="http://x/respond")
    art.MAX_REQUEST_BYTES = 100                       # far below the payload below
    big = [File(file_type="pptx", file_name="big.pptx", file_data=b"\x00" * 5000)]
    with mock.patch("appsci_bench.eval.artifact.urllib.request.urlopen") as urlopen:
        resp = art.respond(ArtifactQuery(intended_task="x", files=big))
    urlopen.assert_not_called()                        # guard fires before any I/O
    assert "exceeds MAX_REQUEST_BYTES" in resp.output["error"]


def test_default_timeout_is_the_query_ceiling_not_30s():
    # 30s would time out an agentic artifact mid-tool-loop, and a files request is far larger now.
    assert HttpArtifact.DEFAULT_TIMEOUT >= 300


def test_the_three_query_ceilings_agree():
    """All three names for the per-query ceiling must carry the same number.

    They are one limit reached by three paths, and they silently drifted apart (180 / 300 / 300).
    Because the orchestrator passes its own value explicitly, the real eval path ran at 300 while
    library callers defaulted to 180 -- so a local `--artifact-dir` rerun could time out two minutes
    EARLIER than the run it was meant to reproduce, and report a timeout the original never had.
    Pinning equality rather than a literal keeps them in step without freezing the value.
    """
    from appsci_bench.eval.generation import DEFAULT_QUERY_TIMEOUT_S
    from appsci_bench.orchestrator.run import BenchmarkOrchestrator

    assert float(HttpArtifact.DEFAULT_TIMEOUT) == float(DEFAULT_QUERY_TIMEOUT_S), (
        "HttpArtifact.DEFAULT_TIMEOUT and generation.DEFAULT_QUERY_TIMEOUT_S must match"
    )
    assert float(BenchmarkOrchestrator.QUERY_TIMEOUT_S) == float(DEFAULT_QUERY_TIMEOUT_S), (
        "BenchmarkOrchestrator.QUERY_TIMEOUT_S must match the library default it overrides"
    )


# --- response side -----------------------------------------------------------------------------

def test_response_parses_top_level_json_and_files():
    pptx = base64.b64encode(b"PPTXBYTES").decode("ascii")
    _, resp = _roundtrip(ArtifactQuery(intended_task="x"), {
        "choices": [{"message": {"content": "made a deck"}}],
        "json": '{"presentation": {"slides": []}}',
        "files": [{"file_type": "pptx", "file_name": "deck.pptx", "file_data": pptx}],
    })
    assert resp.json == '{"presentation": {"slides": []}}'
    assert [f.file_name for f in resp.files] == ["deck.pptx"]
    assert resp.files[0].file_data == b"PPTXBYTES"
    assert resp.messages[-1]["content"] == "made a deck"


def test_response_with_only_choices_yields_no_json_or_files():
    # The pre-existing text-only shape must keep behaving exactly as it did.
    _, resp = _roundtrip(ArtifactQuery(intended_task="x"),
                         {"choices": [{"message": {"content": "just text"}}]})
    assert resp.json is None and resp.files == []
    assert resp.output == {"response": "just text"}


def test_response_with_unusable_file_is_skipped_not_fatal():
    # One bad entry must not lose the good one, nor raise -- the deliverable still parsed.
    good = base64.b64encode(b"ok").decode("ascii")
    _, resp = _roundtrip(ArtifactQuery(intended_task="x"), {
        "choices": [{"message": {"content": "c"}}],
        "files": [{"file_type": "exe", "file_name": "bad.exe", "file_data": good},
                  {"file_type": "pptx", "file_name": "good.pptx", "file_data": good}],
    })
    assert [f.file_name for f in resp.files] == ["good.pptx"]
    # ...and the refused one is recorded, so a report can say "returned but rejected".
    assert resp.rejected_files == [{"file_type": "exe", "file_name": "bad.exe"}]


@pytest.mark.parametrize("label", [
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openai.pptx",
    "some/unlisted-type",
])
def test_response_pptx_mime_label_is_accepted_as_pptx(label):
    # The slides defect: a deck labelled with a MIME type was dropped and scored as "no deck".
    data = base64.b64encode(b"DECK").decode("ascii")
    _, resp = _roundtrip(ArtifactQuery(intended_task="x"), {
        "choices": [{"message": {"content": "c"}}],
        "files": [{"file_type": label, "file_name": "deck.pptx", "file_data": data}],
    })
    assert [(f.file_type, f.file_data) for f in resp.files] == [("pptx", b"DECK")]


def test_response_non_string_json_is_ignored():
    # The contract says `json` is a JSON-encoded STRING; a dict there is not silently accepted.
    _, resp = _roundtrip(ArtifactQuery(intended_task="x"), {
        "choices": [{"message": {"content": "c"}}], "json": {"not": "a string"},
    })
    assert resp.json is None


# --- HttpArtifactClient passthrough ------------------------------------------------------------
# The wire tests above cover HttpArtifact itself. These cover the client that sits on top of it,
# which is where the files-in/files-out defect actually lived: it collapsed the query to text and
# returned only text. A local recording fake keeps test_artifact_client.py's _FakeArtifact untouched.

from appsci_bench.eval.artifact import ArtifactResponse                      # noqa: E402
from appsci_bench.eval.artifact_client import HttpArtifactClient             # noqa: E402
from appsci_bench.eval.datatypes import ArtifactAgentQuery                   # noqa: E402


class _RecordingArtifact:
    """Records the full ArtifactQuery per call (not just history) and returns a scripted response."""

    def __init__(self, response=None):
        self._response = response or ArtifactResponse(
            output=None, messages=[{"role": "assistant", "content": "ok"}])
        self.sent_queries = []

    def respond(self, query):
        self.sent_queries.append(query)
        return self._response


def _client(fake) -> HttpArtifactClient:
    c = HttpArtifactClient("http://x/respond")
    c._artifact = fake
    return c


def test_query_session_passes_files_to_the_artifact():
    # THE regression test for the primary defect: query.files used to be dropped on the floor here,
    # so the artifact was asked to build a deck from grounding documents it never received.
    fake = _RecordingArtifact()
    c = _client(fake)
    sid = c.create_session()
    files = [File(file_type="csv", file_name="metrics.csv", file_data=b"rev,51000")]
    c.query_session(sid, ArtifactAgentQuery(text="build", files=files))
    assert [f.file_name for f in fake.sent_queries[0].files] == ["metrics.csv"]
    assert fake.sent_queries[0].files[0].file_data == b"rev,51000"


def test_query_session_returns_json_and_files():
    # ...and the other half: json/files used to be hardcoded empty on the way back, so a produced
    # .pptx had nowhere to go and the functional gates could never pass.
    out_file = File(file_type="pptx", file_name="deck.pptx", file_data=b"DECK")
    fake = _RecordingArtifact(ArtifactResponse(
        output=None, messages=[{"role": "assistant", "content": "done"}],
        json='{"presentation": {"slides": [1]}}', files=[out_file]))
    c = _client(fake)
    sid = c.create_session()
    out = c.query_session(sid, ArtifactAgentQuery(text="build"))
    assert out.text == "done"
    assert out.json == '{"presentation": {"slides": [1]}}'
    assert [f.file_name for f in out.files] == ["deck.pptx"]
    assert out.files[0].file_data == b"DECK"


def test_query_session_materializes_file_path_backed_files(tmp_path):
    # A dataset File referenced by path carries no bytes until materialized; it must not reach the
    # wire empty just because the caller forgot to materialize.
    p = tmp_path / "src.csv"
    p.write_bytes(b"on disk")
    lazy = File.from_dict({"file_type": "csv", "file_name": "src.csv", "file_path": str(p)})
    assert lazy.file_data == b""
    fake = _RecordingArtifact()
    c = _client(fake)
    sid = c.create_session()
    c.query_session(sid, ArtifactAgentQuery(text="x", files=[lazy]))
    assert fake.sent_queries[0].files[0].file_data == b"on disk"


def test_files_are_not_replayed_on_later_turns():
    # Attachments belong to the turn that carried them: the stored history is text-only, so a
    # multi-MB deck is not re-uploaded every turn of a multi-turn case.
    fake = _RecordingArtifact()
    c = _client(fake)
    sid = c.create_session()
    c.query_session(sid, ArtifactAgentQuery(text="turn1",
                                            files=[File(file_type="csv", file_name="a.csv",
                                                        file_data=b"x")]))
    c.query_session(sid, ArtifactAgentQuery(text="turn2"))
    assert fake.sent_queries[1].files == []
    assert all(isinstance(m["content"], str) for m in fake.sent_queries[1].history)


def test_query_session_drops_invalid_json_framing():
    # Parity with InProcessArtifactClient: a malformed `json` field is dropped (warning logged)
    # rather than failing the whole turn -- a good `files` deliverable alongside it must still score.
    fake = _RecordingArtifact(ArtifactResponse(
        output=None, messages=[{"role": "assistant", "content": "c"}], json="{not valid json"))
    c = _client(fake)
    sid = c.create_session()
    out = c.query_session(sid, ArtifactAgentQuery(text="x"))
    assert out.json is None
    assert out.text == "c"

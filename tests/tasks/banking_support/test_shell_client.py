"""The `shell` tool's workspace-side behaviour: upstream fidelity and honest failure.

Docker-free by construction -- the transport is stubbed, so these cover what the AGENT observes
(refusal wording, sanitized output, the tool's rendering of each exit code) rather than whether the
sidecar confines anything. Confinement is a property of the image and the compose service, asserted
separately; it cannot be unit-tested and should not be faked here.

The escape-pattern checks matter even though our confinement no longer relies on them. Upstream needed
them because its sandbox could not restrict reads at all; we keep them because the refusal string is
agent-visible, and the leaderboard models were shaped by receiving it.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tasks.banking_support.environment import shell_client as sc
from tasks.banking_support.environment._runtime.retrieval_mixins import ShellMixin


class _Tool(ShellMixin):
    """Minimal host for the mixin: the tool only ever reads `self._sandbox`."""

    def __init__(self, sandbox):
        self._sandbox = sandbox


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _shell(payload=None, *, exc=None):
    """A RemoteShell whose transport returns `payload` (or raises `exc`)."""
    shell = sc.RemoteShell("http://shell:8081", token="tok")
    target = "tasks.banking_support.environment.shell_client.urllib.request.urlopen"
    if exc is not None:
        return shell, patch(target, side_effect=exc)
    return shell, patch(target, return_value=_FakeResponse(payload))


# --- escape patterns: refused before anything executes -----------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("cat ../etc/passwd", ".."),
    ("cat /etc/passwd", "absolute path: /etc/passwd"),
    ("echo ~", "~"),
    ("echo $HOME", "$HOME"),
    ("echo ${HOME}", "$HOME"),
])
def test_escape_patterns_are_refused_with_upstreams_wording(command, expected):
    shell, transport = _shell({"exit_code": 0, "stdout": "should never run", "stderr": ""})
    with transport as mock_urlopen:
        rc, out, err = shell.run_command(command)
    assert (rc, out) == (1, "")
    assert err == f"Error: Command blocked - contains '{expected}' which could escape the sandbox"
    mock_urlopen.assert_not_called()      # refused locally; the sidecar never saw it


def test_a_quoted_document_name_is_not_mistaken_for_an_escape_attempt():
    # Quoted spans are blanked before the scan, so a filename containing a flagged character still runs.
    shell, transport = _shell({"exit_code": 0, "stdout": "ok", "stderr": ""})
    with transport:
        rc, out, _ = shell.run_command("grep 'a/b' INDEX.md")
    assert (rc, out) == (0, "ok")


# --- output sanitisation -----------------------------------------------------------------------

def test_ls_long_output_is_normalized_so_it_does_not_vary_between_image_builds():
    # File mtimes come from build time; without this the same corpus yields a different `ls -l`.
    raw = "-rw-r--r-- 1 realuser realgroup 1234 Mar  3 11:22 INDEX.md\n"
    shell, transport = _shell({"exit_code": 0, "stdout": raw, "stderr": ""})
    with transport:
        _, out, _ = shell.run_command("ls -l")
    assert out == "-rw-r--r-- 1 kb_user  kb_group  1234 Jan  1 00:00 INDEX.md\n"
    assert "realuser" not in out and "realgroup" not in out


def test_ordinary_output_passes_through_untouched():
    shell, transport = _shell({"exit_code": 0, "stdout": "# Fees\n\nNo fees.\n", "stderr": ""})
    with transport:
        _, out, _ = shell.run_command("cat doc_b_002.md")
    assert out == "# Fees\n\nNo fees.\n"


# --- how the tool renders each outcome ---------------------------------------------------------

def test_grep_finding_nothing_reads_as_no_matches_not_as_an_error():
    # grep exits 1 on no match. Upstream's tool turns that into prose, and models rely on it.
    shell, transport = _shell({"exit_code": 1, "stdout": "", "stderr": ""})
    with transport:
        assert _Tool(shell).shell("grep -r 'absent' .") == "No matches found."


def test_a_successful_command_with_no_output_says_so():
    shell, transport = _shell({"exit_code": 0, "stdout": "", "stderr": ""})
    with transport:
        assert _Tool(shell).shell("true") == "(no output)"


def test_a_timeout_is_reported_with_its_exit_code_rather_than_silently_empty():
    shell, transport = _shell({"exit_code": 124, "stdout": "",
                               "stderr": "Command timed out after 30 seconds"})
    with transport:
        rendered = _Tool(shell).shell("sleep 999")
    assert "exit code 124" in rendered and "timed out" in rendered


def test_an_uninitialized_shell_still_reports_rather_than_raising():
    # BANKING_SHELL_URL unset -> no driver -> the pre-sidecar behaviour, with bm25/dense/grep unaffected.
    assert _Tool(None).shell("ls") == "Error: Sandbox not initialized"


# --- the backend being down degrades the run, it does not abort the case ------------------------

def test_an_unreachable_sidecar_is_a_command_failure_not_an_exception():
    import urllib.error
    shell, transport = _shell(exc=urllib.error.URLError("connection refused"))
    with transport:
        rc, out, err = shell.run_command("ls")
    assert (rc, out) == (1, "")
    assert "shell backend unavailable" in err


def test_a_malformed_reply_is_also_survived():
    shell, transport = _shell(exc=ValueError("not json"))
    with transport:
        rc, _, err = shell.run_command("ls")
    assert rc == 1 and "shell backend failure" in err


# --- environment wiring ------------------------------------------------------------------------

def test_no_shell_url_means_no_driver_which_is_the_honest_answer(monkeypatch):
    monkeypatch.delenv("BANKING_SHELL_URL", raising=False)
    assert sc.shell_from_env() is None


def test_a_configured_url_builds_a_driver_without_probing_it(monkeypatch):
    # Reachability is decided per call: a sidecar slow to boot must not disable the tool for the run.
    monkeypatch.setenv("BANKING_SHELL_URL", "http://banking-shell-1:8081")
    monkeypatch.setenv("BANKING_SHELL_TOKEN", "tok")
    assert isinstance(sc.shell_from_env(), sc.RemoteShell)

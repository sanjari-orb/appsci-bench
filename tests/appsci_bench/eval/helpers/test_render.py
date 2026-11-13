"""Unit tests for helpers/render.py. Entirely mocked -- no real soffice invocation anywhere in
this suite, per the decision to keep CI free of a LibreOffice install step.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from appsci_bench.eval.helpers import render as render_module
from appsci_bench.eval.helpers.render import render_pptx_to_pdf


def test_returns_none_with_a_reason_when_soffice_is_not_found():
    with patch.object(render_module, "_find_soffice", return_value=None), \
         patch("subprocess.run") as mock_run:
        pdf_file, reason = render_pptx_to_pdf(b"fake pptx bytes")
    assert pdf_file is None
    assert "soffice" in reason.lower()
    mock_run.assert_not_called()


def test_returns_none_with_a_reason_when_conversion_fails():
    with patch.object(render_module, "_find_soffice", return_value="/usr/bin/soffice"), \
         patch("subprocess.run",
               side_effect=subprocess.CalledProcessError(1, ["soffice"], stderr=b"boom")):
        pdf_file, reason = render_pptx_to_pdf(b"fake pptx bytes")
    assert pdf_file is None
    assert "conversion failed" in reason


def test_returns_none_with_a_reason_on_timeout():
    with patch.object(render_module, "_find_soffice", return_value="/usr/bin/soffice"), \
         patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd=["soffice"], timeout=30.0)):
        pdf_file, reason = render_pptx_to_pdf(b"fake pptx bytes", timeout_s=30.0)
    assert pdf_file is None
    assert "timed out" in reason


def test_returns_none_with_a_reason_when_soffice_cannot_be_started():
    # soffice existed at _find_soffice() time but isn't runnable (permission stripped, corrupt
    # install, removed mid-race) -- subprocess.run raises an OSError subclass, not a
    # subprocess.CalledProcessError/TimeoutExpired. Must degrade to (None, reason), not raise.
    with patch.object(render_module, "_find_soffice", return_value="/usr/bin/soffice"), \
         patch("subprocess.run", side_effect=PermissionError("not executable")):
        pdf_file, reason = render_pptx_to_pdf(b"fake pptx bytes")
    assert pdf_file is None
    assert "not be started" in reason


def test_every_call_converts_results_are_not_cached():
    # Regression guard for the removed content-hash cache: it could never hit for the decks it was
    # written for (callers re-save a trimmed deck before rendering, which restamps timestamps and
    # changes the bytes), so rendering is now unconditional. Two calls with IDENTICAL bytes must
    # therefore run two conversions -- if this starts asserting once, caching came back.
    data = b"fake pptx bytes"

    def _fake_run(cmd, **kwargs):
        # write a fake pdf next to the input so render_pptx_to_pdf finds it
        from pathlib import Path
        src = cmd[-1]
        Path(src).with_suffix(".pdf").write_bytes(b"%PDF-1.4 fake")
        return None

    with patch.object(render_module, "_find_soffice", return_value="/usr/bin/soffice"), \
         patch("subprocess.run", side_effect=_fake_run) as mock_run:
        first, first_reason = render_pptx_to_pdf(data)
        second, second_reason = render_pptx_to_pdf(data)
    assert first_reason is None and second_reason is None
    assert first.file_data == second.file_data == b"%PDF-1.4 fake"
    assert mock_run.call_count == 2


def test_returns_a_pdf_file_on_success():
    def _fake_run(cmd, **kwargs):
        from pathlib import Path
        src = cmd[-1]
        Path(src).with_suffix(".pdf").write_bytes(b"%PDF-1.4 real-looking")
        return None

    with patch.object(render_module, "_find_soffice", return_value="/usr/bin/soffice"), \
         patch("subprocess.run", side_effect=_fake_run):
        pdf_file, reason = render_pptx_to_pdf(b"fake pptx bytes")
    assert reason is None
    assert pdf_file.file_type == "pdf"
    assert pdf_file.file_data == b"%PDF-1.4 real-looking"


def test_each_call_gets_its_own_libreoffice_user_profile():
    # Regression guard: concurrent `soffice --headless` calls sharing a profile/lock race and one
    # silently produces no output PDF (reproduced directly against a real template.pptx before this
    # fix). Each invocation must pass a distinct -env:UserInstallation profile scoped to its own
    # temp dir, never a shared/fixed path two concurrent calls could collide on.
    seen_profiles = []

    def _fake_run(cmd, **kwargs):
        from pathlib import Path
        profile_arg = next(a for a in cmd if a.startswith("-env:UserInstallation="))
        seen_profiles.append(profile_arg)
        src = cmd[-1]
        Path(src).with_suffix(".pdf").write_bytes(b"%PDF-1.4 fake")
        return None

    with patch.object(render_module, "_find_soffice", return_value="/usr/bin/soffice"), \
         patch("subprocess.run", side_effect=_fake_run):
        render_pptx_to_pdf(b"deck one")
        render_pptx_to_pdf(b"deck two")
    assert len(seen_profiles) == 2
    assert seen_profiles[0] != seen_profiles[1]

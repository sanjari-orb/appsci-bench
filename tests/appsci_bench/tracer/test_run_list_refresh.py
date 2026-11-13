"""The run list keeps itself current without throwing the page away.

Regression: this page reloaded the whole document every 10 seconds. That is a visible flash, and it
also dropped the scroll position, whatever was typed in the search box, and any error message under
it -- which is why the reload had to be suppressed in three separate cases to stay usable at all. The
rows still come from the server (scores, mirror state and replicate grouping are all decided there);
they are patched into the page instead of navigated to.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from appsci_bench.tracer.server import TracerServer


def _html(runs_dir) -> str:
    return TestClient(TracerServer(runs_dir=runs_dir, port=0)._create_app()).get("/").text


def _seed(runs_dir, tid: str = "r0") -> None:
    d = runs_dir / tid
    d.mkdir()
    (d / "events.jsonl").write_text(
        '{"event_id": "e1", "trace_id": "%s", "event_type": "x", "source": "s", '
        '"timestamp": "2026-08-25T00:00:00Z", "payload": {}}\n' % tid
    )


def test_the_page_never_reloads_itself(tmp_path):
    _seed(tmp_path)
    assert "location.reload" not in _html(tmp_path)


def test_the_swappable_region_wraps_the_table_and_not_the_search_box(tmp_path):
    """What gets replaced must not include the search box or its status line: replacing those is how
    a reader loses what they were typing, or the error they were reading."""
    _seed(tmp_path)
    html = _html(tmp_path)

    region_at = html.index('id="run-list-region"')
    assert html.index('id="run-id-input"') < region_at
    assert html.index('id="run-search-status"') < region_at
    assert region_at < html.index('class="run-list"')


def test_the_region_is_present_with_no_runs_at_all(tmp_path):
    """An empty runs dir renders the empty state, and an update has to be able to replace THAT with a
    table once the first run shows up."""
    assert 'id="run-list-region"' in _html(tmp_path)


def test_an_update_rebinds_the_group_toggles(tmp_path):
    """The rows the click handlers were bound to are gone after a swap, so the binding has to be
    re-runnable rather than a one-shot at load."""
    _seed(tmp_path)
    html = _html(tmp_path)

    assert "function initReplicateGroups" in html
    assert html.count("initReplicateGroups()") >= 2, "bound at load AND after each update"

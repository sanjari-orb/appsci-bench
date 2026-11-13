"""Hostile-input properties of the tracer's routes, as one file because they share a cause.

The tracer used to be a standalone port a developer opened on their own box. It is now mounted at
`/tracer` inside the experiments server, so every route here answers on the dashboard's origin with
the dashboard's session cookie attached. Each test below is one thing that was fine on a private port
and is not fine there:

* the two `docker exec` file routes were dead code (unreachable *and* broken) on an always-on server;
* a run id went from the URL straight into a JS template literal, where HTML autoescape does nothing;
* a run id was joined onto the runs dir with no confinement, so `?trace_id=../x` escaped it;
* a NUL byte in `?path=` came back as an unhandled 500 rather than a 404;
* a directory listing followed symlinks when it stat'd, turning it into a size oracle for host paths;
* `/docs` loaded Swagger UI from a CDN, i.e. a third-party script on the cookie's origin.

No Docker, no network.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from appsci_bench.tracer import file_server
from appsci_bench.tracer.server import TracerServer


def _client(runs_dir):
    return TestClient(TracerServer(runs_dir=runs_dir, port=0)._create_app())


def _write_run(runs_dir, trace="good_run_20260812"):
    run = runs_dir / trace
    run.mkdir(parents=True)
    run.joinpath("events-orchestrator.jsonl").write_text(json.dumps({
        "event_id": "e1", "source": "orchestrator", "event_type": "build.start",
        "trace_id": trace, "task_id": "airline_rebooking",
        "timestamp": "2026-08-12T12:00:00+00:00", "payload": {},
    }) + "\n")
    return trace


# ---------------------------------------------------------------------------
# The `docker exec` file routes are gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/files/some-container/list",
    "/api/files/some-container/list?path=/workspace",
    "/api/files/some-container?path=/workspace/x",
    "/api/files/some-container?path=workspace/x",
])
def test_the_container_file_routes_no_longer_exist(tmp_path, path):
    """404 from the router, not 422/404 from a handler.

    Both were unusable before they were deleted: `sanitize_path` rejected every absolute path (so the
    list route's own `/workspace` default failed), and the greedy `{container_name:path}` route
    shadowed `/list` -- which answered 422 for a missing `path` query param it never wanted. Asserting
    on the *absence* of the route rather than on those symptoms, since the point is that no request
    reaches a `docker exec` from this server at all.
    """
    assert _client(tmp_path).get(path).status_code == 404


def test_the_container_readers_are_gone_from_the_module():
    """Deleted, not merely unrouted -- a future route must not be able to reintroduce the surface."""
    for name in ("get_file_from_container", "list_directory", "sanitize_path", "ALLOWED_PATHS"):
        assert not hasattr(file_server, name), f"{name} is still importable"


# ---------------------------------------------------------------------------
# Run ids: traversal, and JS template-literal injection
# ---------------------------------------------------------------------------

#: Every route that takes a run id. `/api/s3/pull` already validated; the rest did not.
ID_ROUTES = [
    "/api/harness/{}",
    "/api/events/{}",
    "/api/events/{}/stream",
    "/api/summary/{}",
    "/api/artifact/{}",
    "/api/artifact/{}/file?path=x",
    "/api/agent_fs/{}",
    "/api/agent_fs/{}/file?path=x",
    "/api/eval/{}",
    "/api/eval/{}/report_json",
    "/api/eval/{}/conversations",
    "/eval/{}/report",
    "/api/model_io/{}?ref=abc",
]

#: Ids that must never reach a filesystem join or a template. `..%2F..%2Fx` is here because a bare
#: `../` is normalised away by the HTTP layer before routing, so it never tests the handler.
HOSTILE_IDS = ["..", "..%2F..%2Fx", "%2e%2e%2fx", "bad id", "x/y", "-leading-dash"]


@pytest.mark.parametrize("route", ID_ROUTES)
def test_a_hostile_run_id_is_a_400_on_every_id_route(tmp_path, route):
    c = _client(tmp_path)
    for bad in HOSTILE_IDS:
        code = c.get(route.format(bad)).status_code
        # 404 is acceptable only when the router itself refused the shape (a literal `/` in the id
        # cannot match a single path segment); anything that reaches a handler must be a 400.
        assert code in (400, 404), f"{route.format(bad)} answered {code}"


def test_the_dashboard_refuses_a_traversing_trace_id(tmp_path):
    """`?trace_id=../<dir>` used to answer 200 and render a directory outside the runs dir."""
    outside = tmp_path / "outside_run"
    outside.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    r = _client(runs).get("/", params={"trace_id": "../outside_run"})
    assert r.status_code == 400
    assert "Invalid run id" in r.text


def test_a_backtick_run_id_never_reaches_the_dashboard_template(tmp_path):
    """The template-literal hole, closed at the id.

    `{{ trace_id }}` is interpolated inside ``fetch(`{{ base }}/api/events/{{ trace_id }}`)``. Jinja
    autoescapes for HTML, which leaves a backtick and `${` untouched, so a run *directory* named with
    a payload rendered as script -- reproduced at 200 with the payload raw in eleven template
    literals. The directory is created here so this fails for the right reason: the id is refused,
    not merely absent.
    """
    payload = "x`+document.cookie+`"
    (tmp_path / payload).mkdir()
    r = _client(tmp_path).get("/", params={"trace_id": payload})
    assert r.status_code == 400
    # The 400 echoes the id, which is fine -- it is a JSON body, and the search box renders an error
    # with `textContent`. What must not happen is the *dashboard* rendering, so assert on the
    # template-literal context the payload was aimed at rather than on the payload itself.
    assert "application/json" in r.headers["content-type"]
    assert "/api/events/" not in r.text


def test_a_dollar_brace_run_id_is_refused_too(tmp_path):
    payload = "x${alert(1)}"
    (tmp_path / payload).mkdir()
    r = _client(tmp_path).get("/", params={"trace_id": payload})
    assert r.status_code == 400
    assert "/api/events/" not in r.text


def test_a_legitimate_run_id_still_renders(tmp_path):
    """The guard must not cost the normal case: real ids are `<task>_<YYYYmmdd>_<HHMMSS>`."""
    trace = _write_run(tmp_path)
    r = _client(tmp_path).get("/", params={"trace_id": trace})
    assert r.status_code == 200 and trace in r.text


# ---------------------------------------------------------------------------
# `?path=` on the snapshot readers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["/api/artifact/{}/file", "/api/agent_fs/{}/file"])
def test_a_nul_byte_in_the_path_is_a_404_not_a_500(tmp_path, route):
    """Every `os` call on a path with a NUL raises a bare ValueError, which is not a FileServerError.

    It therefore escaped the route's `except` and came back as an unhandled 500. `%00` survives a
    query string intact, so this needed no unusual client.
    """
    trace = _write_run(tmp_path)
    (tmp_path / trace / "artifact").mkdir()
    (tmp_path / trace / "agent_fs").mkdir()
    r = _client(tmp_path).get(route.format(trace), params={"path": "a\x00b"})
    assert r.status_code == 404
    assert "NUL" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Directory listings and symlinks
# ---------------------------------------------------------------------------


def test_a_listing_does_not_report_a_symlink_target_size(tmp_path):
    """A bare `stat()` follows the link, so the listing leaked the size of an arbitrary host file."""
    secret = tmp_path / "host_secret.bin"
    secret.write_bytes(b"S" * 98_765)
    art = tmp_path / "artifact"
    art.mkdir()
    (art / "real.txt").write_text("hello")
    (art / "link_to_secret").symlink_to(secret)

    entries = file_server.list_artifact_files(art)
    assert [e["path"] for e in entries] == ["real.txt"]
    assert 98_765 not in [e["size"] for e in entries]


def test_a_symlinked_file_is_not_readable_either(tmp_path):
    """The listing and the reader agree: a link out of the snapshot is refused, so omitting it from
    the listing hides nothing that could have been opened."""
    secret = tmp_path / "host_secret.bin"
    secret.write_text("token")
    art = tmp_path / "artifact"
    art.mkdir()
    (art / "link_to_secret").symlink_to(secret)
    with pytest.raises(file_server.FileServerError):
        file_server.read_artifact_file(art, "link_to_secret")


# ---------------------------------------------------------------------------
# The stored eval report
# ---------------------------------------------------------------------------


def test_the_eval_report_is_served_into_an_opaque_origin(tmp_path):
    """The one route that returns stored HTML raw, now that it is same-origin with the dashboard.

    `eval/report_html.py` escapes every dynamic value at one chokepoint, so this is defence in depth
    rather than a fix for a live hole -- but the report embeds model and judge output, and a field
    that ever skips that chokepoint would otherwise execute with the dashboard's session cookie.
    `sandbox allow-scripts` keeps the report's own inline script working while denying it the origin.
    """
    trace = _write_run(tmp_path)
    ev = tmp_path / trace / "eval"
    ev.mkdir(parents=True)
    ev.joinpath("report.html").write_text("<h1>report</h1>")
    r = _client(tmp_path).get(f"/eval/{trace}/report")
    assert r.status_code == 200 and "<h1>report</h1>" in r.text
    csp = r.headers["content-security-policy"]
    assert "sandbox" in csp and "allow-scripts" in csp
    # Not `allow-same-origin`: that would hand the document the dashboard's origin back.
    assert "allow-same-origin" not in csp


# ---------------------------------------------------------------------------
# The auto-generated API docs
# ---------------------------------------------------------------------------

DOC_ROUTES = ["/docs", "/redoc", "/openapi.json"]


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_the_api_docs_are_disabled_standalone(tmp_path, path):
    """`/docs` served Swagger UI from `cdn.jsdelivr.net/npm/swagger-ui-dist@5`.

    A third-party script on a floating major version, executing on the origin that can `POST
    /api/jobs` and cancel jobs with the dashboard's HttpOnly session cookie attached -- which is the
    same reason the dashboard's own CDN dependencies were removed. The experiments app already
    disables all three; this pins the half that is mounted inside it.
    """
    assert _client(tmp_path).get(path).status_code == 404


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_the_api_docs_are_disabled_when_mounted_too(tmp_path, path):
    """The tracer runs standalone on its own port AND mounted at `/tracer`; both use `_create_app`.

    Pinned separately because a mount is a different object to the ASGI stack, and it is the mounted
    case -- sharing an origin with the dashboard's cookie -- where the CDN script actually matters.
    """
    from fastapi import FastAPI

    outer = FastAPI()
    outer.mount("/tracer", TracerServer(runs_dir=tmp_path, port=0)._create_app())
    assert TestClient(outer).get(f"/tracer{path}").status_code == 404


def test_no_route_serves_a_cdn_script(tmp_path):
    """The property behind the two tests above, stated once: nothing this app returns loads a script
    from another host. Cheaper to keep true than to rediscover after someone adds a `FastAPI()` with
    default `docs_url`.
    """
    c = _client(tmp_path)
    trace = _write_run(tmp_path)
    for path in DOC_ROUTES + ["/", f"/?trace_id={trace}"]:
        body = c.get(path).text
        assert "cdn.jsdelivr.net" not in body, f"{path} pulls a script from jsdelivr"

"""Native history is read-only, bounded, paginated, and independent of AppSci events."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from appsci_bench.tracer import harness_history as history
from appsci_bench.tracer.server import TracerServer
from shared.observability import redact


def write_run(tmp_path, records, *, adapter="codex"):
    run = tmp_path / "native_run"
    bundle = run / "harness"
    bundle.mkdir(parents=True)
    (bundle / "launch.json").write_text(json.dumps({"adapter": adapter}))
    (bundle / "prompt.md").write_text("Build the requested artifact.")
    (bundle / "native.log").write_text("".join(json.dumps(record) + "\n" for record in records))
    return run


def command(kind, call_id="cmd1", **extra):
    return {"type": kind, "item": {"type": "command_execution", "id": call_id, **extra}}


def test_codex_messages_calls_and_responses_keep_native_order_and_ids(tmp_path):
    run = write_run(tmp_path, [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Inspecting the task."}},
        command("item.started", command="cat /spec/spec.yaml", status="in_progress"),
        command("item.started", "cmd2", command="false"),
        command("item.completed", "cmd2", aggregated_output="error", exit_code=1),
        command("item.completed", aggregated_output="task spec", exit_code=0),
        {"type": "item.completed", "item": {"type": "file_change", "id": "patch1",
                                             "changes": [{"path": "/work/out/server.py", "kind": "add"}]}},
        {"type": "turn.failed", "error": {"message": "Interrupted"}},
    ])
    result = history.read_history(run)
    assert result["harness"] == "codex" and result["available"]
    assert result["prompt"] == "Build the requested artifact."
    rows = result["entries"]
    assert [r["kind"] for r in rows] == ["status", "assistant", "tool_call", "tool_call",
                                        "tool_result", "tool_result", "tool_result", "notice"]
    assert [r["call_id"] for r in rows[2:6]] == ["cmd1", "cmd2", "cmd2", "cmd1"]
    assert rows[4]["status"] == "exit 1"
    assert rows[5]["content"] == "task spec"
    assert all(r["timestamp"] is None for r in rows)  # Never invent legacy timestamps.
    assert not result["has_more"]


def test_claude_assistant_tool_use_and_user_tool_result(tmp_path):
    # Claude Code SDKMessage assistant/user envelopes and Anthropic content blocks.
    run = write_run(tmp_path, [
        {"type": "system", "subtype": "init", "model": "test-model"},
        {"type": "assistant", "parent_tool_use_id": "parent1", "message": {"content": [
            {"type": "text", "text": "Running a check."},
            {"type": "tool_use", "id": "tool1", "name": "Bash", "input": {"command": "pytest"}},
        ]}},
        {"type": "user", "parent_tool_use_id": "parent1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool1", "content": "2 passed", "is_error": False},
        ]}},
        {"type": "result", "subtype": "success", "result": "Finished"},
    ], adapter="claude-code")
    rows = history.read_history(run)["entries"]
    assert [r["kind"] for r in rows] == ["status", "assistant", "tool_call", "tool_result", "status"]
    assert rows[2]["call_id"] == rows[3]["call_id"] == "tool1"
    assert rows[2]["parent_call_id"] == rows[3]["parent_call_id"] == "parent1"
    assert rows[3]["content"] == "2 passed"


def test_pagination_live_append_and_partial_line_do_not_drop_or_duplicate(tmp_path):
    run = write_run(tmp_path, [command("item.started", command="sleep 1"),
                               command("item.completed", exit_code=0)])
    first = history.read_history(run, limit=1)
    assert first["has_more"]
    second = history.read_history(run, offset=first["offset"], limit=1)
    assert not second["has_more"]
    assert first["entries"][0]["id"] != second["entries"][0]["id"]
    log = run / "harness/native.log"
    with log.open("a") as stream:
        stream.write('{"type":"turn.')
    partial = history.read_history(run, offset=second["offset"])
    assert partial["entries"] == [] and partial["offset"] == second["offset"]
    with log.open("a") as stream:
        stream.write('completed"}\n')
    assert len(history.read_history(run, offset=partial["offset"])["entries"]) == 1


def test_timestamped_live_logs_stderr_and_truncation_reset(tmp_path):
    run = write_run(tmp_path, [])
    log = run / "harness/native.log"
    log.write_text('2026-09-16T12:00:00.123456789Z {"type":"turn.started"}\nwarning from CLI\n')
    result = history.read_history(run)
    assert result["entries"][0]["timestamp"] == "2026-09-16T12:00:00.123456789Z"
    assert result["entries"][1]["content"] == "warning from CLI"
    log.write_text('{"type":"turn.started"}\n')
    assert history.read_history(run, offset=result["offset"])["reset"]


def test_hostile_shapes_large_records_and_known_credentials(tmp_path, monkeypatch):
    secret = "secret-test-value-long-enough-to-redact"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    redact.reset_cache()
    run = write_run(tmp_path, [[], {"type": []}, {"type": "assistant", "message": None},
                               {"type": "item.completed", "item": {"type": "agent_message", "text": secret}}])
    try:
        assert secret not in json.dumps(history.read_history(run))
    finally:
        redact.reset_cache()
    monkeypatch.setattr(history, "MAX_LINE_BYTES", 64)
    log = run / "harness/native.log"
    log.write_text("x" * 200 + '\n{"type":"turn.started"}\n')
    result = history.read_history(run)
    assert result["entries"][0]["title"] == "Oversized log record omitted"
    assert result["entries"][1]["title"] == "turn.started"
    monkeypatch.setattr(history, "MAX_LOG_BYTES", 64)
    assert history.read_history(run)["warnings"]


@pytest.mark.parametrize("target", ["run", "harness", "log"])
def test_symlinks_cannot_serve_files_outside_the_run(tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "native.log").write_text("private data\n")
    run = tmp_path / "native_run"
    if target == "run":
        run.symlink_to(outside, target_is_directory=True)
    else:
        run.mkdir()
        if target == "harness":
            (run / "harness").symlink_to(outside, target_is_directory=True)
        else:
            (run / "harness").mkdir()
            (run / "harness/native.log").symlink_to(outside / "native.log")
    assert not history.read_history(run)["available"]


def test_api_mount_validation_and_appSci_empty_history(tmp_path):
    write_run(tmp_path, [command("item.started", command="echo hello")])
    app = FastAPI()
    app.mount("/tracer", TracerServer(runs_dir=tmp_path)._create_app())
    client = TestClient(app)
    response = client.get("/tracer/api/harness/native_run")
    assert response.status_code == 200
    assert response.json()["entries"][0]["content"] == "echo hello"
    assert client.get("/tracer/api/harness/internal_run").json()["available"] is False
    assert client.get("/tracer/api/harness/native_run?offset=-1").status_code == 422
    assert client.get("/tracer/api/harness/native_run?offset=" + "9" * 100).status_code == 422
    assert client.get("/tracer/api/harness/native_run?limit=10000").status_code == 422
    assert client.get("/tracer/api/harness/bad%60id").status_code == 400
    page = client.get("/tracer/?trace_id=native_run").text
    assert "/tracer/api/harness/native_run" in page
    assert 'x-text="entry.content"' in page
    assert 'x-html="entry.content"' not in page
    assert "Load more history" not in page
    assert '<template x-if="entry.expanded"><pre x-text="entry.content">' in page
    assert page.index("/tracer/static/harness-history.js") < page.index("/tracer/static/alpine-")
    assert client.get("/tracer/static/harness-history.js").status_code == 200


def test_missing_live_log_is_explicit(tmp_path):
    run = write_run(tmp_path, [])
    (run / "harness/native.log").unlink()
    result = history.read_history(run)
    assert result["available"] and "Older workers" in result["warnings"][0]


@pytest.fixture
def run_dashboard_js(tmp_path):
    """Execute the shipped scripts with deterministic timers, without adding JS dependencies."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the dashboard JavaScript")
    write_run(tmp_path, [])
    client = TestClient(TracerServer(runs_dir=tmp_path)._create_app())
    page = client.get("/?trace_id=native_run").text
    script = client.get("/static/harness-history.js").text + "\n"
    script += "\n".join(re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", page, re.S))
    setup = """
const assert = require('assert');
global.document = {hidden: false};
const timers = new Map();
let timerId = 0;
global.setTimeout = (callback, delay) => {
    timers.set(++timerId, {callback, delay});
    return timerId;
};
global.clearTimeout = id => timers.delete(id);
const reply = data => ({ok: true, json: async () => ({
    available: true, harness: 'codex', entries: [], offset: 0,
    prompt: null, has_more: false, warnings: [], ...data,
})});
async function nextTimer(delay) {
    assert.equal(timers.size, 1, 'one outstanding history timer');
    const [id, timer] = [...timers][0];
    assert.equal(timer.delay, delay);
    timers.delete(id);
    await timer.callback();
}
"""

    def run(body):
        program = setup + script + "\n(async () => {\n" + body + """
})().catch(e => {console.error(e); process.exit(1);});
"""
        result = subprocess.run([node, "-e", program], text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr

    return run


def test_dashboard_native_history_client_runs_and_links_tools(run_dashboard_js):
    run_dashboard_js("""
    const d = dashboard();
    const rows = [
      {id:'0:0', kind:'tool_call', call_id:'__proto__', content:'echo hi'},
      {id:'1:0', kind:'tool_result', call_id:'__proto__', content:'hi'}
    ];
    global.fetch = async () => ({ok:true, json:async () => ({available:true, harness:'codex',
      entries:rows, offset:10, prompt:null, has_more:false, warnings:[]})});
    await d.loadNativeHistory();
    await d.loadNativeHistory();
    assert.equal(d.native.entries.length, 2);
    assert.equal(d.nativeCallId(rows[1]), '0:0');
    assert.equal(d.native.entries[0].status, 'completed');
    assert.equal(d.nativeRole(rows[1]), 'Tool response');
    d.events = [{event_type:'build.complete', payload:{status:'completed'}}];
    assert.equal(d.nativeBuildStatus(), 'Build outcome: completed');
    document.hidden = true;
    global.fetch = () => {throw Error('hidden tab must not poll');};
    await d.loadNativeHistory();
    d.destroy();
    assert.equal(d.nativeDisposed, true);
    assert.equal(timers.size, 0);
""")


def test_dashboard_automatically_fetches_every_page_then_polls(run_dashboard_js):
    run_dashboard_js("""
    const d = dashboard();
    const rows = Array.from({length: 104}, (_, i) => ({id: i+':0', kind: 'assistant', content: 'message '+i}));
    const offsets = [];
    global.fetch = async url => {
        const offset = Number(new URL(url, 'http://tracer').searchParams.get('offset'));
        offsets.push(offset);
        return reply({entries: rows.slice(offset, offset+100), offset: Math.min(104, offset+100),
            has_more: offset+100 < 104});
    };
    await d.loadNativeHistory();
    assert.equal(d.native.total, 100);
    assert.equal(d.native.hasMore, true);
    await nextTimer(25); // No button click: consume the timer scheduled by the first page.
    assert.equal(d.native.total, 104);
    assert.equal(d.native.messageCount, 104);
    assert.equal(d.native.entries.at(-1).content, 'message 103');
    assert.equal(d.native.hasMore, false);
    await nextTimer(3000);
    assert.deepEqual(offsets, [0, 100, 104]);
    assert.equal(d.native.total, 104);
    d.destroy();
""")


@pytest.mark.parametrize("failure", ["http", "network", "stalled"])
def test_dashboard_backlog_retries_without_spinning_or_losing_rows(run_dashboard_js, failure):
    run_dashboard_js("const failure = " + json.dumps(failure) + ";\n" + """
    const d = dashboard();
    global.fetch = async () => reply({entries: [{id: '0:0', kind: 'tool_call', call_id: 'cmd'}],
        offset: 10, has_more: true});
    await d.loadNativeHistory();
    global.fetch = async () => {
        if (failure === 'http') return {ok: false, status: 503};
        if (failure === 'network') throw Error('Network failure');
        return reply({offset: 10, has_more: true});
    };
    await nextTimer(25);
    assert.ok(d.native.error);
    assert.equal(d.native.offset, 10);
    assert.equal(d.native.total, 1);
    global.fetch = async url => {
        assert.ok(url.endsWith('offset=10'));
        return reply({entries: [{id: '10:0', kind: 'tool_result', call_id: 'cmd', status: 'exit 0'}], offset: 20});
    };
    await nextTimer(3000);
    assert.equal(d.native.error, null);
    assert.equal(d.native.total, 2);
    assert.equal(d.native.entries[0].status, 'exit 0');
    assert.equal(d.nativeCallId(d.native.entries[1]), '0:0');
    d.destroy();
""")


def test_dashboard_pauses_and_discards_aborted_pages_and_resets_rotated_logs(run_dashboard_js):
    run_dashboard_js("""
    const d = dashboard();
    let resolve;
    global.fetch = () => new Promise(done => {resolve = done;});
    const pending = d.loadNativeHistory();
    d.evalData.loaded = true;
    d.switchTab('eval'); // Abort a page already in flight.
    resolve(reply({entries: [{id: 'stale'}], offset: 100, has_more: true}));
    await pending;
    assert.equal(d.native.total, 0);
    assert.equal(d.native.offset, 0);
    assert.equal(timers.size, 0);
    global.fetch = () => {throw Error('inactive tabs must not fetch');};
    await d.loadNativeHistory();
    d.activeTab = 'build';
    document.hidden = true;
    await d.loadNativeHistory();
    document.hidden = false;
    global.fetch = async () => reply({entries: [{id: '100:0', kind: 'assistant'}], offset: 1000});
    await d.loadNativeHistory();
    d.nativeToggle('100:0', true);
    global.fetch = async () => reply({reset: true, entries: [{id: '0:0', kind: 'status'}], offset: 10});
    await nextTimer(3000);
    assert.equal(d.native.total, 1);
    assert.equal(d.native.entries[0].id, '0:0');
    assert.equal(d.native.messageCount, 0);
    assert.equal(d.nativeStore.window.expanded.size, 0);
    assert.equal(d.nativeStore.window.indices.has('100:0'), false);
    d.destroy();
""")


def test_large_history_keeps_only_a_window_rendered_and_preserves_links_and_expansion(run_dashboard_js):
    run_dashboard_js("""
    const d = dashboard();
    const history = d.nativeStore.window;
    const rows = Array.from({length: 10000}, (_, i) => ({id: i+':0', kind: 'assistant', content: 'message '+i}));
    rows[0] = {id: '0:0', kind: 'tool_call', call_id: '__proto__', content: 'x'.repeat(5000)};
    rows[9999] = {id: '9999:0', kind: 'tool_result', call_id: '__proto__', content: 'done', status: 'exit 0'};
    const viewport = {scrollTop: 0, clientHeight: 600};
    d.$refs = {nativeViewport: viewport};
    for (let i = 0; i < rows.length; i += 100) history.append(rows.slice(i, i+100));
    history.append(rows); // Re-fetching must not duplicate entries or counts.
    d.nativeRender();
    assert.equal(d.native.total, 10000);
    assert.equal(d.native.messageCount, 9998);
    assert.ok(d.native.entries.length < 30);
    assert.equal(d.native.entries[0].expanded, false);
    assert.equal(d.native.entries[0].status, 'exit 0');
    d.nativeToggle('0:0', true);
    history.measure([['0:0', 320], ['1:0', 80]]);
    assert.equal(history.topFor('2:0'), 400);
    assert.equal(history.indexAt(399), 1);
    assert.equal(history.indexAt(400), 2);
    viewport.scrollTop = 800000;
    d.nativeRender();
    assert.ok(d.native.paddingTop > 0 && d.native.paddingBottom > 0);
    assert.ok(d.native.entries.length < 30);
    assert.ok(!d.native.entries.some(row => row.id === '0:0'));
    d.nativeJumpToLatest();
    assert.equal(d.native.entries.at(-1).id, '9999:0');
    assert.equal(d.native.paddingBottom, 0);
    const call = d.nativeCallId(d.native.entries.at(-1));
    assert.equal(call, '0:0');
    d.nativeJumpTo(call);
    assert.equal(viewport.scrollTop, 0);
    assert.equal(d.native.entries[0].expanded, true);
    d.nativeToggle('0:0', false);
    d.nativeRender();
    assert.equal(d.native.entries[0].expanded, false);
    assert.equal(d.nativeStore.window.entries.length, 10000);
    d.destroy();
""")


def test_measuring_real_row_heights_preserves_visible_anchor_and_cleans_up(run_dashboard_js):
    run_dashboard_js("""
    const d = dashboard();
    const history = d.nativeStore.window;
    history.append(Array.from({length: 30}, (_, i) => ({id: i+':0', kind: 'assistant'})));
    // Rows are actually 80px tall, not the initial 160px estimate. At scrollTop=320,
    // row 4 is visible; anchoring to estimated row 2 would incorrectly jump upward.
    const viewport = {
        scrollTop: 320, clientWidth: 1000, clientHeight: 600, scrollHeight: 2400,
        getBoundingClientRect: () => ({top: 0}),
        querySelectorAll: () => history.entries.map((entry, i) => ({
            dataset: {nativeId: entry.id},
            getBoundingClientRect: () => ({top: i*80-viewport.scrollTop,
                bottom: (i+1)*80-viewport.scrollTop, height: 80}),
        })),
    };
    d.$refs = {nativeViewport: viewport};
    let observations = 0, disconnected = 0;
    global.ResizeObserver = class {
        observe() {observations++;}
        disconnect() {disconnected++;}
    };
    d.$nextTick = callback => callback();
    d.nativeRender();
    d.nativeMeasure();
    assert.equal(viewport.scrollTop, 320);
    assert.equal(history.topFor('4:0'), 320);
    const observed = observations;
    d.nativeMeasure();
    assert.equal(observations, observed, 'unchanged measurements must not cause a render loop');
    viewport.scrollTop = 1800;
    d.nativeMeasure();
    assert.equal(viewport.scrollTop, 1800, 'stay at bottom');
    let scheduled = 0, cancelled = null;
    global.requestAnimationFrame = () => ++scheduled;
    global.cancelAnimationFrame = id => {cancelled = id;};
    d.nativeScheduleMeasure();
    d.nativeScheduleMeasure();
    assert.equal(scheduled, 1, 'coalesce scroll/resize events into one frame');
    const disconnects = disconnected;
    d.destroy();
    assert.equal(cancelled, 1);
    assert.equal(disconnected, disconnects+1);
""")

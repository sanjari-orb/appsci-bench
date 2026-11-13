#!/usr/bin/env python
"""Auto-detect task leakage from a build/eval run trace (#77).

Complements the STATIC guardrail in `appsci_bench/core/spec.py` (`agent_spec_leaks`), which checks
the redacted spec.yaml + the repo's `environment/{data,sop}` dirs *before* a run happens. This tool
checks an actual completed run's trace for leakage that only shows up at RUNTIME:

  1. mounted_privileged_file  - the wrong files got mounted into the agent-under-test container,
                                 giving it access to ground-truth (tasks/, gold/, eval_data/,
                                 scripts/, environment/{db,data,setup}) paths or content.
  2. context_leak             - task details (SOP text, gold answer, "ground truth" / "reward_basis"
                                 / oracle wording) ended up in what the agent-under-test's model
                                 actually saw (its prompts/completions).
  3. eval_pointer_leak        - a pointer into the eval dataset (evaluation_data.dataset path, or a
                                 ground-truth resource's host `location`) got transmitted to the
                                 agent under test.

Three detection layers, applied to every scanned surface (the third to mounted files only):
  * generic, task-agnostic markers (`_RUNTIME_FORBIDDEN` below) -- a deliberately narrowed variant
    of `appsci_bench.core.spec._AGENT_SPEC_FORBIDDEN`: that regex's bare `sop/` prefix is correct
    for scanning a rendered spec.yaml TEXT view (ground truth only ever shows up there as a
    seed-corpus path like "sop/policy.md"), but every real surface THIS tool scans
    legitimately mentions the agent's OWN `/sop` mount by name -- a mounted file's path contains
    "sop/", and the agent's own exploration output (an `ls /` it ran) does too. `sop/` is dropped
    here for that reason; see `_RUNTIME_FORBIDDEN`'s own docstring.
  * task-specific literal content (verbatim `gold_solution` / `evaluation_data.dataset`
    / resource `location` strings pulled from THIS run's spec.yaml).
  * content-hash identity against the task's real `evaluation_data.dataset` / `gold_solution.path`
    files on disk -- catches an exact-copy leak mounted under an innocuous name/path with no marker
    words in its content, which the first two layers can't see.

Only surfaces that represent what the agent-under-test actually had are scanned: its filesystem
snapshot (`agent_fs/appsci_agent/`), the deployed artifact snapshot (`artifact/`), and its own model
calls (`model_calls-appsci_agent.jsonl` / `model_calls-mini_swe_agent.jsonl`). The user-sim's mounted
files (`agent_fs/usersim/`) and its model calls are a TRUSTED surface (it legitimately knows the raw
spec) and are never scanned.

Run trace directory shape (`runs/<trace_id>/`, see `appsci_bench/orchestrator/run.py` +
`shared/observability/`):
    events-<source>.jsonl         # per-source event log; every event carries `task_id`
    model_calls-<source>.jsonl    # full LLM request/response records (ModelCall)
    agent_fs/appsci_agent/        # snapshot of everything mounted into / written by the agent
    agent_fs/usersim/             # user-sim's own mounted files -- TRUSTED, not scanned
    artifact/                     # deployed artifact snapshot

Usage:
    .venv/bin/python scripts/detect_task_leakage.py path/to/runs/<trace_id>
    .venv/bin/python scripts/detect_task_leakage.py path/to/runs/<trace_id> --spec-path tasks/x/spec.yaml
    .venv/bin/python scripts/detect_task_leakage.py path/to/runs/<trace_id> --no-write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from appsci_bench.core.spec import resolve_spec_path  # noqa: E402

# Sources that represent what the agent under test actually had access to. Everything else
# (usersim, artifact_usersim, orchestrator, evaluator, task_env) is a trusted, ground-truth-aware
# component.
AGENT_UNDER_TEST_SOURCES = {"appsci_agent", "mini_swe_agent"}

# Snapshot dirs, relative to the trace dir, that hold what the agent under test could see.
AGENT_SNAPSHOT_DIRS = ("agent_fs/appsci_agent", "artifact")

# Literal-snippet strings shorter than this are too generic to search for verbatim (avoids noisy
# matches on e.g. a bare "true" or a short common word pulled from a ground-truth field).
MIN_LITERAL_LEN = 8

# Skip obviously-binary files by extension when walking a filesystem snapshot.
_BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz", ".pyc", ".db", ".sqlite"}

# Marker set for RUNTIME scanning (both a mounted file's own path/content, and model context) --
# deliberately NOT `appsci_bench.core.spec._AGENT_SPEC_FORBIDDEN` reused verbatim. That regex's
# bare `sop/` prefix is correct for its own job (scanning a rendered spec.yaml TEXT view, where
# "sop/" only ever shows up as a ground-truth path like "sop/policy.md"), but
# every one of THIS tool's real surfaces legitimately mentions the agent's OWN `/sop` mount by name:
# a mounted file lives at a path containing "sop/" (see core/spec.py's "what is and isn't gated" --
# SOP/policy docs are intentionally exposed to the agent), and the agent's own exploration output
# (an `ls /` or `ls -la /sop` it ran and wrote into its own report) routinely contains the bare
# token "sop/" too. Reusing the text-oriented marker here flagged both on every real trace tested.
# `tasks/|gold/|seed_data/|eval_data/|scripts/`, the environment/{db,data,setup} host-dir markers, and the
# wording markers below have no such legitimate agent-mount collision, so they're unchanged.
_RUNTIME_FORBIDDEN = re.compile(
    r"""(?xi)
      \b(?:tasks|gold|seed_data|eval_data|scripts)/
    | \benvironment/(?:db|data|setup)\b
    | gated\s+to\s+L
    | ground[\s-]?truth
    | reference\s+solution
    | golden\s+action
    | \breward_basis\b | \bnl_assertion | \boracle\.data\b
    """
)


# --- spec IO --------------------------------------------------------------------------------------

def load_raw_spec(*, task_id: str | None = None, spec_path: pathlib.Path | str | None = None) -> dict:
    """Load a task's raw spec.yaml as a plain dict (no schema validation -- callers only need the
    ground-truth fields, and validation would reject a deliberately-minimal test fixture)."""
    import yaml
    path = pathlib.Path(spec_path) if spec_path else resolve_spec_path(task_id)
    return yaml.safe_load(path.read_text())


def _collect_strings(obj) -> list[str]:
    """Recursively collect every string leaf under a nested dict/list structure."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        out = []
        for v in obj.values():
            out.extend(_collect_strings(v))
        return out
    if isinstance(obj, list):
        out = []
        for v in obj:
            out.extend(_collect_strings(v))
        return out
    return []


def literal_snippets(raw_spec: dict) -> dict[str, str]:
    """Verbatim ground-truth strings worth searching for, mapped to the failure_mode they imply.

    Pulled from THIS run's spec.yaml so the check is task-specific: a generic marker regex catches
    structural leaks (paths, oracle wording) but misses e.g. the literal text of a task's SOP or
    gold answer showing up in the agent's context.
    """
    out: dict[str, str] = {}

    def add(strings: list[str], failure_mode: str) -> None:
        for s in strings:
            s = s.strip()
            if len(s) >= MIN_LITERAL_LEN:
                out[s] = failure_mode

    add(_collect_strings(raw_spec.get("gold_solution") or {}), "context_leak")
    evaluation_data = raw_spec.get("evaluation_data") or {}
    dataset = evaluation_data.get("dataset")
    if isinstance(dataset, str):
        add([dataset], "eval_pointer_leak")
    # The elicitation rubric is answer-key material in the same tier as the dataset: it is the
    # concentrated list of what the agent was supposed to have to ASK for, with the true answers
    # written out. Both the pointer and the facts themselves are worth searching for -- a fact turning
    # up in the agent's context means it was handed something it was scored on asking for, which would
    # inflate the diagnostic without any objective noticing.
    rubric_pointer = evaluation_data.get("elicitation_rubric")
    if isinstance(rubric_pointer, str):
        add([rubric_pointer], "eval_pointer_leak")
        try:
            add(_elicitation_facts(rubric_pointer), "context_leak")
        except RubricScanError:
            # Costs only this layer -- every other surface is still scanned. The run is NOT clean
            # though: rubric_scan_problem() reports the same failure as a finding, so an unreadable
            # rubric can never be mistaken for "searched for the facts and found none".
            pass
    # A resource's `location` is only added when it points at the answer key itself
    # (eval_data/gold). Most resources' `location` is legitimately re-delivered to the agent
    # through its own mount (e.g. compliance_review's `policy_kb` resource has
    # location: "/data/policies/" -- that IS the agent's own mount point) or through
    # task_runner.agent_env (e.g. a sandboxed DB connection string the agent is meant to use).
    # Flagging every resource location as a leak would false-positive on exactly the
    # channels the task's own environment wiring uses on purpose; the static redactor already
    # strips `location` from the spec TEXT for a narrower reason (don't pre-reveal the shortcut
    # before the agent explores), which this runtime check must not conflate with "must never
    # be agent-visible by any legitimate channel".
    for resource in ((raw_spec.get("environment") or {}).get("resources") or []):
        location = resource.get("location")
        if isinstance(location, str) and ("eval_data" in location or "gold" in location):
            add([location], "eval_pointer_leak")
    return out


class RubricScanError(RuntimeError):
    """A declared elicitation rubric could not be read, so its facts were never searched for.

    Raised rather than returned empty: a spec that names a rubric has no legitimate empty answer here,
    and the alternative is the worst outcome this tool has -- printing "clean" having checked nothing,
    so a one-character indent slip or a renamed file turns a real leak into a green tick.
    """


def _elicitation_facts(pointer: str) -> list[str]:
    """Every `fact` in the rubric at `pointer`, for verbatim searching.

    Read straight from the YAML rather than through the loader: this tool has to keep working against a
    rubric that no longer validates, and a task bug is not a reason to stop looking for leaks. That
    tolerance covers a rubric this tool could not *validate*, never one it could not *read*.
    """
    path = REPO_ROOT / pointer
    if not path.is_file():
        raise RubricScanError(
            f"elicitation_rubric points at {pointer}, which does not exist -- the fact layer of the "
            "scan cannot run"
        )
    try:
        import yaml

        document = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001 -- unreadable is not the same as invalid; see the class
        raise RubricScanError(f"elicitation_rubric {pointer} is unparseable: {exc}") from exc
    facts = [item["fact"] for item in (document.get("items") or [])
             if isinstance(item, dict) and isinstance(item.get("fact"), str)]
    if not facts:
        raise RubricScanError(f"elicitation_rubric {pointer} yielded no `fact` strings")
    return facts


def _hash_file(path: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def ground_truth_file_hashes(raw_spec: dict) -> dict[str, tuple[str, str]]:
    """Content hash -> (original relpath, failure_mode) for every REAL file under this task's
    held-out eval dataset (`evaluation_data.dataset`) and reference solution (`gold_solution.path`).

    A marker/literal-snippet scan only catches a leak that still LOOKS like ground truth (a
    forbidden path, "ground truth" wording, verbatim SOP/gold text). It misses the exact same held-
    out file copied into the agent's mount under an innocuous name with no such wording (e.g.
    tasks/foo/eval_data/cases.jsonl mounted as data/cases.jsonl) -- a real miss of failure mode 1.
    Hashing the actual ground-truth files once and comparing every mounted file's content hash
    catches that regardless of filename, at the cost of missing a leak that's been re-encoded/
    re-saved with different bytes but the same content.
    """
    out: dict[str, tuple[str, str]] = {}

    def add_dir(rel_dir: str, failure_mode: str) -> None:
        root = REPO_ROOT / rel_dir
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            digest = _hash_file(path)
            if digest:
                out[digest] = (str(path.relative_to(REPO_ROOT)), failure_mode)

    def add_file(rel_path: str, failure_mode: str) -> None:
        path = REPO_ROOT / rel_path
        if not path.is_relative_to(REPO_ROOT):
            raise RubricScanError(f"{rel_path} resolves outside the repo ({path})")
        digest = _hash_file(path) if path.is_file() else None
        if digest:
            out[digest] = (str(path.relative_to(REPO_ROOT)), failure_mode)

    evaluation_data = raw_spec.get("evaluation_data") or {}
    dataset = evaluation_data.get("dataset")
    if isinstance(dataset, str):
        add_dir(dataset, "eval_pointer_leak")
    gold_path = (raw_spec.get("gold_solution") or {}).get("path")
    if isinstance(gold_path, str):
        add_dir(gold_path, "mounted_privileged_file")
    # The rubric copied into the agent's mount under an innocuous name is the leak the marker and
    # literal layers are worst at: it is one small YAML file, and renaming it removes every structural
    # tell. Its hash does not care what it is called.
    rubric_pointer = evaluation_data.get("elicitation_rubric")
    if isinstance(rubric_pointer, str):
        add_file(rubric_pointer, "mounted_privileged_file")
    return out


# --- classification --------------------------------------------------------------------------------

def _classify_marker(marker: str) -> str:
    """Map one `_RUNTIME_FORBIDDEN` match to a #77 failure_mode."""
    m = marker.lower()
    if "eval_data" in m or "gold" in m:
        return "eval_pointer_leak"
    if m.startswith(("tasks/", "scripts/")) or m.startswith("environment/"):
        return "mounted_privileged_file"
    return "context_leak"


def scan_text(text: str, literals: dict[str, str]) -> list[tuple[str, str]]:
    """Return sorted, unique (marker_or_snippet, failure_mode) matches in `text`."""
    if not text:
        return []
    found: dict[str, str] = {}
    for m in _RUNTIME_FORBIDDEN.finditer(text):
        marker = m.group(0).strip()
        found[marker] = _classify_marker(marker)
    for snippet, failure_mode in literals.items():
        if snippet in text:
            found[snippet] = failure_mode
    return sorted(found.items())


def scan_path(rel_path: str, literals: dict[str, str]) -> list[tuple[str, str]]:
    """Alias for `scan_text` when the "text" being scanned is a mounted file's own relative
    path, not its content -- kept as a separate name so call sites read as intent (mount
    structure vs. content), even though both now use the same `_RUNTIME_FORBIDDEN` marker set."""
    return scan_text(rel_path, literals)


# --- surfaces --------------------------------------------------------------------------------------

def _make_finding(surface: str, location: str, marker: str, failure_mode: str) -> dict:
    excerpt = marker if len(marker) <= 200 else marker[:200] + "..."
    return {"surface": surface, "location": location, "marker": excerpt, "failure_mode": failure_mode}


def check_mounted_files(trace_dir: pathlib.Path, literals: dict[str, str],
                         gt_hashes: dict[str, tuple[str, str]] | None = None) -> list[dict]:
    """Scan every file the agent under test could see (path + content + content-hash identity)
    for leak markers/snippets/exact copies."""
    gt_hashes = gt_hashes or {}
    findings: list[dict] = []
    for rel_root in AGENT_SNAPSHOT_DIRS:
        root = trace_dir / rel_root
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(trace_dir))
            # Path scanning needs no file read, so it must run even for a binary we won't open below
            # -- a leaked path (e.g. agent_fs/appsci_agent/data/gold/answer.pdf) is a real finding
            # regardless of whether we can/should read its content.
            for marker, failure_mode in scan_path(rel, literals):
                findings.append(_make_finding("mounted_file:path", rel, marker, failure_mode))
            # Content-hash identity check: runs for EVERY file (binary or text), since it's a byte
            # comparison, not a text scan -- this is what catches a real held-out file mounted
            # under an innocuous name/path with no marker words in its content.
            if gt_hashes:
                digest = _hash_file(path)
                match = gt_hashes.get(digest) if digest else None
                if match:
                    original_rel, failure_mode = match
                    findings.append(_make_finding("mounted_file:content-hash", rel,
                                                   f"identical to {original_rel}", failure_mode))
            if path.suffix.lower() in _BINARY_EXTS:
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            for marker, failure_mode in scan_text(text, literals):
                findings.append(_make_finding("mounted_file:content", rel, marker, failure_mode))
    return findings


def _load_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _stringify_messages(messages) -> str:
    parts = []
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else msg
        parts.append(content if isinstance(content, str) else json.dumps(content, default=str))
    return "\n".join(parts)


def _stringify_tool_calls(tool_calls) -> str:
    # ModelCall.tool_calls is [{"name": ..., "arguments": {...}}, ...] -- a leaked ground-truth
    # path/string commonly shows up here (e.g. a bash tool call's "cat tasks/x/eval_data/y.jsonl"
    # command), not in the message text, so it needs its own scan target.
    return json.dumps(tool_calls, default=str) if tool_calls else ""


def check_model_context(trace_dir: pathlib.Path, literals: dict[str, str]) -> list[dict]:
    """Scan the agent-under-test's own model calls (prompt + completion + tool calls) for leak
    markers/snippets."""
    from shared.observability.model_io import load_model_calls
    findings: list[dict] = []
    calls = load_model_calls(trace_dir)
    for call_id, call in calls.items():
        if call.get("source") not in AGENT_UNDER_TEST_SOURCES:
            continue
        text = "\n".join([
            _stringify_messages(call.get("messages")),
            call.get("content") or "",
            _stringify_tool_calls(call.get("tool_calls")),
        ])
        location = f"model_calls-{call.get('source')}.jsonl:{call_id}"
        for marker, failure_mode in scan_text(text, literals):
            findings.append(_make_finding("model_context", location, marker, failure_mode))
    return findings


def resolve_task_id(trace_dir: pathlib.Path) -> str | None:
    """The task_id is denormalized onto every observability event; read the first one we find."""
    for events_path in sorted(trace_dir.glob("events-*.jsonl")):
        for event in _load_jsonl(events_path):
            task_id = event.get("task_id")
            if task_id:
                return task_id
    # Fallback for a trace with no readable events: recover the task id from the directory name.
    #
    # Run names carry OPTIONAL middle components -- `<task>_[model]_[label]_[persona]_<date>_<time>`
    # (see `compose_run_name` in orchestrator/run.py) -- so the old `rsplit("_", 2)[0]` returned
    # everything before the timestamp, e.g. `airline_rebooking_claude-opus-4-8_b0.t1_l3`. Task ids
    # themselves contain underscores, so the name cannot be split on them at all. Match against the
    # known task directories instead, longest first so `order_to_cash` never loses to a shorter id
    # that happens to prefix it.
    name = trace_dir.name
    try:
        known = sorted(
            (p.name for p in (REPO_ROOT / "tasks").iterdir() if p.is_dir()),
            key=len,
            reverse=True,
        )
    except OSError:
        known = []
    for task_id in known:
        if name == task_id or name.startswith(task_id + "_"):
            return task_id
    return None


# --- top-level --------------------------------------------------------------------------------------

def rubric_scan_problem(raw_spec: dict) -> str | None:
    """Why the elicitation-fact layer could not run, or None when it ran.

    Separate from `literal_snippets` on purpose: that function must keep scanning every other surface
    when a rubric is unreadable (a task bug is not a reason to stop looking for leaks), but the scan
    must not then report clean. This is what turns the silent skip into a finding -- the worst outcome
    this tool has is printing "no leakage detected" having never searched for the facts.
    """
    pointer = (raw_spec.get("evaluation_data") or {}).get("elicitation_rubric")
    if not isinstance(pointer, str):
        return None
    try:
        _elicitation_facts(pointer)
    except RubricScanError as exc:
        return str(exc)
    return None


def detect_leakage(trace_dir: pathlib.Path, spec_path: pathlib.Path | str | None = None) -> list[dict]:
    task_id = None if spec_path else resolve_task_id(trace_dir)
    if spec_path is None and task_id is None:
        raise ValueError(f"could not resolve a task_id for trace {trace_dir}; pass --spec-path")
    raw_spec = load_raw_spec(task_id=task_id, spec_path=spec_path)
    literals = literal_snippets(raw_spec)
    gt_hashes = ground_truth_file_hashes(raw_spec)
    findings = check_mounted_files(trace_dir, literals, gt_hashes) + check_model_context(trace_dir, literals)
    findings.sort(key=lambda f: (f["surface"], f["location"], f["marker"]))
    problem = rubric_scan_problem(raw_spec)
    if problem:
        findings.append({"surface": "scan", "failure_mode": "rubric_unreadable",
                         "location": "evaluation_data.elicitation_rubric", "marker": problem})
    return findings


def findings_table(trace_dir: pathlib.Path, findings: list[dict]) -> str:
    head = f"### task leakage report — {trace_dir.name}\n\n"
    if not findings:
        return head + "no leakage detected.\n"
    rows = ["| Surface | Failure mode | Location | Marker/snippet |", "|---|---|---|---|"]
    for f in findings:
        rows.append(f"| {f['surface']} | {f['failure_mode']} | {f['location']} | `{f['marker']}` |")
    return head + "\n".join(rows) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Auto-detect task leakage from a run trace (#77).")
    ap.add_argument("trace_dir", type=pathlib.Path)
    ap.add_argument("--spec-path", type=pathlib.Path, default=None,
                    help="explicit spec.yaml path (skips task_id resolution / tasks-dir lookup)")
    ap.add_argument("--no-write", action="store_true", help="print only; do not write leakage_report.json")
    args = ap.parse_args(argv)
    if not args.trace_dir.exists():
        sys.exit(f"no trace dir at {args.trace_dir}")

    findings = detect_leakage(args.trace_dir, spec_path=args.spec_path)

    out = {"trace_id": args.trace_dir.name, "clean": not findings, "findings": findings}
    if not args.no_write:
        (args.trace_dir / "leakage_report.json").write_text(json.dumps(out, indent=2))
    print(findings_table(args.trace_dir, findings))
    print(f"{len(findings)} leak(s) found" if findings else "clean: no leakage detected")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

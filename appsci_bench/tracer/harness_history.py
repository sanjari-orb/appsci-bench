"""Read native CLI JSONL as a paginated, display-only conversation.

Supports saved Codex exec --json and Claude Code --output-format stream-json logs.
These are harness events, NOT benchmark model-call/cost records. Never infer missing
messages, timestamps, or usage. Older native.log files have no Docker timestamp.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from shared.observability.redact import scrub

MAX_LINE_BYTES = 1024 * 1024
MAX_READ_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 32_000
_DOCKER_TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\S+) ")


def _text(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = scrub(text)
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS] + "\n[Display truncated]"
    return text


def _row(kind: str, title: str, content: Any = "", *, call_id: Any = None,
         status: Any = None, parent_call_id: Any = None) -> dict:
    return {"kind": kind, "title": _text(title), "content": _text(content),
            "call_id": _text(call_id) if call_id is not None else None,
            "status": _text(status) if status is not None else None,
            "parent_call_id": _text(parent_call_id) if parent_call_id is not None else None}


def normalize_event(event: dict) -> list[dict]:
    """Keep native ordering and native tool IDs, including parallel tool responses."""
    kind = event.get("type")
    if kind in {"item.started", "item.completed", "item.updated"}:
        item = event.get("item")
        if not isinstance(item, dict):
            return [_row("notice", "Malformed native item")]
        item_type = item.get("type")
        if kind == "item.updated":
            return []  # Cumulative updates are superseded by the completed record.
        if item_type in {"agent_message", "reasoning"}:
            if kind != "item.completed":
                return []
            return [_row("assistant", "Assistant" if item_type == "agent_message" else "Reasoning summary",
                         item.get("text", ""))]
        call_id = item.get("id")
        started = kind == "item.started"
        row_kind = "tool_call" if started else "tool_result"
        if item_type == "command_execution":
            content = item.get("command", "") if started else item.get("aggregated_output", "")
            status = item.get("status")
            if not started and item.get("exit_code") is not None:
                status = f"exit {item['exit_code']}"
            return [_row(row_kind, "Terminal", content, call_id=call_id, status=status)]
        if item_type == "file_change":
            return [_row(row_kind, "File changes", item.get("changes", []), call_id=call_id,
                         status=item.get("status"))]
        if item_type in {"mcp_tool_call", "web_search", "collab_tool_call"}:
            content = item.get("arguments", item.get("query", item)) if started else item.get(
                "result", item.get("error", item))
            return [_row(row_kind, str(item.get("tool", item_type)), content,
                         call_id=call_id, status=item.get("status"))]
        return [_row("notice", str(item_type or "Native item"), item)]
    if kind in {"assistant", "user"}:
        message = event.get("message")
        if not isinstance(message, dict):
            return [_row("notice", "Malformed native message")]
        blocks = message.get("content", [])
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        if not isinstance(blocks, list):
            return [_row("notice", "Malformed message content")]
        rows = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            parent = event.get("parent_tool_use_id")
            if block_type == "tool_use":
                rows.append(_row("tool_call", str(block.get("name", "Tool")), block.get("input", {}),
                                 call_id=block.get("id"), parent_call_id=parent))
            elif block_type == "tool_result":
                rows.append(_row("tool_result", "Tool response", block.get("content", ""),
                                 call_id=block.get("tool_use_id"),
                                 status="error" if block.get("is_error") else "completed",
                                 parent_call_id=parent))
            elif block_type in {"text", "thinking"}:
                rows.append(_row(kind, "Reasoning summary" if block_type == "thinking" else kind.title(),
                                 block.get("text", block.get("thinking", "")), parent_call_id=parent))
            else:
                rows.append(_row("notice", str(block_type or "Content block"),
                                 "Non-text content; not rendered."))
        return rows
    if kind == "system":
        return [_row("status", "Harness session", {key: event[key] for key in
                     ("subtype", "model", "session_id", "claude_code_version") if key in event})]
    if kind == "result":
        return [_row("status", "Harness result", event.get("result", event.get("errors", "")),
                     status=event.get("subtype"))]
    if kind in {"thread.started", "turn.started", "turn.completed", "turn.failed", "error",
                "benchmark.log_warning"}:
        return [_row("notice" if kind in {"error", "turn.failed", "benchmark.log_warning"} else "status",
                     str(kind), event.get("message", event.get("error", event.get("usage", ""))))]
    if kind == "stream_event":
        return []  # Full assistant/user messages already include these partial token deltas.
    return [_row("notice", str(kind or "Unknown native event"), event)]


def _safe_file(run_dir: Path, name: str) -> Path | None:
    """Fixed filenames only; refuse symlinked run/harness/files, including outside the run."""
    path = run_dir / "harness" / name
    if run_dir.is_symlink() or path.parent.is_symlink() or path.is_symlink():
        return None
    return path if path.is_file() else None


def read_history(run_dir: Path, *, offset: int = 0, limit: int = 100) -> dict:
    result = {"available": False, "harness": None, "entries": [], "offset": offset,
              "has_more": False, "reset": False, "warnings": [], "prompt": None}
    launch = _safe_file(run_dir, "launch.json")
    if launch:
        try:
            with launch.open("rb") as stream:
                data = json.loads(stream.read(64 * 1024))
            if isinstance(data, dict):
                name = data.get("adapter") or data.get("name")
                result["harness"] = _text(name) if name is not None else None
                result["available"] = True
        except (OSError, ValueError):
            result["warnings"].append("Harness launch metadata is unreadable.")
    log = _safe_file(run_dir, "native.log")
    if log is None:
        if result["available"]:
            result["warnings"].append("No native log yet. Older workers save it only when the run ends.")
        return result
    result["available"] = True
    with log.open("rb") as stream:
        size = log.stat().st_size
        if offset > size:
            offset = 0
            result["reset"] = True
        if offset == 0:
            prompt = _safe_file(run_dir, "prompt.md")
            if prompt:
                with prompt.open("rb") as source:
                    result["prompt"] = _text(source.read(MAX_TEXT_CHARS + 1).decode("utf-8", errors="replace"))
        stream.seek(offset)
        records = 0
        while records < limit and stream.tell() - offset < MAX_READ_BYTES and stream.tell() < MAX_LOG_BYTES:
            position = stream.tell()
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES:
                # Bound memory even for a hostile line without a newline.
                while not line.endswith(b"\n") and stream.tell() < MAX_LOG_BYTES:
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                rows = [_row("notice", "Oversized log record omitted")]
                timestamp = None
            else:
                if not line.endswith(b"\n"):
                    stream.seek(position)  # A live writer may still be appending this record.
                    break
                raw = line.decode("utf-8", errors="replace").rstrip("\r\n")
                match = _DOCKER_TIMESTAMP.match(raw)
                timestamp = match.group(1) if match else None
                raw = raw[match.end():] if match else raw
                try:
                    event = json.loads(raw)
                    rows = normalize_event(event) if isinstance(event, dict) else [_row("notice", "Native log", raw)]
                except (ValueError, TypeError, RecursionError):
                    rows = [_row("notice", "Native log", raw)]
            for index, row in enumerate(rows):
                result["entries"].append({**row, "id": f"{position}:{index}", "timestamp": timestamp})
            records += 1
        result["offset"] = min(stream.tell(), MAX_LOG_BYTES)
        result["has_more"] = stream.tell() < min(size, MAX_LOG_BYTES) and records > 0
        if size > MAX_LOG_BYTES:
            result["warnings"].append("Native log exceeds the display scan limit.")
    return result

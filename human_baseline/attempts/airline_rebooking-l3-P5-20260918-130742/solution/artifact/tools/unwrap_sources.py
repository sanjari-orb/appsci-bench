#!/usr/bin/env python3
"""Strip a stray ``[{"text": "..."}]`` JSON envelope from source files.

Some tooling paths wrap written file content in a single-element JSON array. That produces a file whose
first line is ``[{"text": "..."`` instead of Python. This script detects that envelope and rewrites the
file with the decoded content. It is idempotent and a no-op on clean files.

Usage:
    python3 tools/unwrap_sources.py [PATH ...]      # default: every *.py under the repo root
"""
from __future__ import annotations

import json
import pathlib
import sys

PREFIX = '[{"text": "'
SUFFIX = '", "type": "text"}]'


def unwrap_once(raw: str) -> tuple[str, bool]:
    """Return (content, changed). Strips one layer of envelope if present."""
    stripped = raw.lstrip()
    if not stripped.startswith(PREFIX):
        return raw, False
    body = stripped[len(PREFIX):]
    end = body.rfind(SUFFIX)
    if end != -1:
        body = body[:end]
    try:
        # Fast path: the body is a well-formed JSON string body.
        return json.loads('"' + body + '"'), True
    except json.JSONDecodeError:
        # Mixed content (escapes plus real newlines): decode the escapes we emit ourselves.
        return body.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\"), True


def unwrap_file(path: pathlib.Path) -> int:
    raw = path.read_text()
    layers = 0
    while True:
        raw, changed = unwrap_once(raw)
        if not changed:
            break
        layers += 1
    if layers:
        path.write_text(raw)
    return layers


def main(argv: list[str]) -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    targets = [pathlib.Path(a) for a in argv[1:]] or sorted(root.rglob("*.py"))
    total = 0
    for path in targets:
        if not path.is_file():
            continue
        layers = unwrap_file(path)
        if layers:
            total += 1
            print(f"unwrapped {layers}x: {path}")
    print(f"{total} file(s) rewritten" if total else "all files clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Turn an incoming /respond request into: the instruction text, the template, and the content docs.

The request carries the attached files (the template + the content docs) in a top-level `files`
array, each a File object:

    {"messages": [...], "files": [{"file_type": "pptx", "file_name": "template.pptx",
                                   "file_data": "<base64>"}, ...]}

So the PRIMARY path reads `body["files"]` and each item's `file_name` / `file_data` (base64). The
other forms below are kept only as tolerant fallbacks (alternative key spellings, inline message
content parts, filesystem paths/dirs), so a slightly different delivery still works:

  1. body["files"] (primary) / ["attachments"] / ["documents"]: File objects
     {file_type, file_name, file_data-as-base64}; also accepts name/data/... spellings.
  2. Inline message content parts (base64 / data URIs).
  3. Filesystem paths named in the message text.
  4. A conventional inputs directory (env SLIDES_INPUT_DIR, else common defaults).

Everything is normalised to `IngestedFile`. The template is identified by the name `template.pptx`
(fallback: a .pptx whose slides are mostly empty placeholders). Files whose bytes cannot be decoded
are recorded in `IngestResult.unreadable` (not raised); the caller reports a missing/unreadable
template or unreadable attachments as a graceful "file not readable" reply.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("slidesgen.ingest")

ALLOWED_CONTENT_EXT = {"docx", "pdf", "pptx", "csv", "xlsx", "xlsm"}
_PATH_RE = re.compile(r"[\w./\-]+\.(?:pptx|docx|pdf|csv|xlsx|xlsm)", re.IGNORECASE)
_DEFAULT_INPUT_DIRS = ["/work/inputs", "/data/inputs", "/data/request", "/data"]


@dataclass
class IngestedFile:
    name: str
    data: bytes
    mime: str = ""

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower().lstrip(".")


@dataclass
class IngestResult:
    instruction: str
    template: Optional[IngestedFile]
    content_docs: List[IngestedFile] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    # Names of attached files whose bytes could not be decoded/read (base64 failed, etc.).
    unreadable: List[str] = field(default_factory=list)


class IngestError(RuntimeError):
    pass


class FileNotReadableError(RuntimeError):
    """An attached file could not be read or parsed. The message is user-facing (returned as the
    assistant's reply), not an internal error."""


# --------------------------------------------------------------------------- helpers


def _b64(data_str: str) -> Optional[bytes]:
    s = data_str
    if s.startswith("data:") and "," in s:            # data URI
        s = s.split(",", 1)[1]
    try:
        return base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        return None


def _instruction_from_messages(messages: List[Dict[str, Any]]) -> str:
    texts = []
    for msg in messages:
        if msg.get("role") not in ("user", "system"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                    texts.append(part.get("text", ""))
    return "\n\n".join(t for t in texts if t and t.strip()).strip()


def _file_from_struct(item: Dict[str, Any]) -> Optional[IngestedFile]:
    """Normalise one File object from the request's `files` array.

    The contract keys are `file_name` and `file_data` (base64), so those are checked FIRST; the other
    spellings (name/filename, data/content/..., path) are tolerant fallbacks for alternative shapes.
    """
    name = (item.get("file_name") or item.get("name") or item.get("filename")
            or os.path.basename(item.get("path") or item.get("file_path") or ""))
    mime = (item.get("mime") or item.get("mime_type") or item.get("media_type")
            or item.get("file_type") or "")
    # inline base64 — `file_data` is the contract key.
    for key in ("file_data", "data", "content", "base64", "bytes"):
        if isinstance(item.get(key), str):
            raw = _b64(item[key])
            if raw:
                return IngestedFile(name=name or "attachment", data=raw, mime=mime)
    # path on disk (fallback)
    path = item.get("path") or item.get("file_path")
    if path and os.path.isfile(path):
        with open(path, "rb") as fh:
            return IngestedFile(name=name or os.path.basename(path), data=fh.read(), mime=mime)
    return None


def _looks_like_file_descriptor(item: Dict[str, Any]) -> bool:
    """True if a dict clearly represents a file, so a decode failure counts as an unreadable file
    (rather than an unrelated object we should quietly ignore)."""
    return any(k in item for k in ("file_data", "data", "content", "base64", "bytes",
                                   "path", "file_path", "file_name", "file_type", "filename"))


def _files_from_content_parts(messages: List[Dict[str, Any]]) -> List[IngestedFile]:
    out: List[IngestedFile] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            src = part.get("source") if isinstance(part.get("source"), dict) else None
            if src and src.get("type") == "base64" and isinstance(src.get("data"), str):
                raw = _b64(src["data"])
                if raw:
                    mt = src.get("media_type", "")
                    name = part.get("name") or part.get("title") or f"attachment.{_ext_for_mime(mt)}"
                    out.append(IngestedFile(name=name, data=raw, mime=mt))
            elif ptype == "image_url":
                url = part.get("image_url", {})
                url = url.get("url") if isinstance(url, dict) else url
                if isinstance(url, str) and url.startswith("data:"):
                    raw = _b64(url)
                    if raw:
                        out.append(IngestedFile(name="image", data=raw, mime="image/png"))
            elif ptype in ("input_file", "file"):
                fobj = part.get("file", part)
                f = _file_from_struct(fobj) if isinstance(fobj, dict) else None
                if f:
                    out.append(f)
    return out


def _ext_for_mime(mime: str) -> str:
    m = (mime or "").lower()
    table = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "text/csv": "csv",
    }
    return table.get(m, "bin")


def _files_from_paths(text: str) -> List[IngestedFile]:
    out: List[IngestedFile] = []
    seen = set()
    for m in _PATH_RE.findall(text or ""):
        if m in seen or not os.path.isfile(m):
            continue
        seen.add(m)
        with open(m, "rb") as fh:
            out.append(IngestedFile(name=os.path.basename(m), data=fh.read()))
    return out


def _files_from_input_dir() -> List[IngestedFile]:
    dirs = []
    env = os.environ.get("SLIDES_INPUT_DIR")
    if env:
        dirs.append(env)
    dirs.extend(_DEFAULT_INPUT_DIRS)
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        found = []
        for root, _, files in os.walk(d):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower().lstrip(".")
                if ext in ALLOWED_CONTENT_EXT:
                    p = os.path.join(root, fn)
                    with open(p, "rb") as fh:
                        found.append(IngestedFile(name=fn, data=fh.read()))
        # Only accept a directory that actually looks like a request (has a template).
        if any(f.name.lower() == "template.pptx" for f in found):
            return found
    return []


def _looks_like_template(data: bytes) -> bool:
    try:
        from pptx import Presentation

        prs = Presentation(io.BytesIO(data))
        slides = list(prs.slides)
        if not slides:
            return True
        empty = 0
        for s in slides:
            has_text = any(
                sh.has_text_frame and sh.text_frame.text.strip() for sh in s.shapes
            )
            if not has_text:
                empty += 1
        return empty >= max(1, len(slides) // 2)
    except Exception:
        return False


def _dedupe(files: List[IngestedFile]) -> List[IngestedFile]:
    out, seen = [], set()
    for f in files:
        key = (f.name.lower(), len(f.data))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


# --------------------------------------------------------------------------- entrypoint


def ingest(body: Dict[str, Any]) -> IngestResult:
    messages = body.get("messages", []) or []
    instruction = _instruction_from_messages(messages)
    notes: List[str] = []

    files: List[IngestedFile] = []
    unreadable: List[str] = []

    # 1. Structured file lists on the body. A descriptor we can't decode is recorded as unreadable.
    for key in ("files", "attachments", "documents"):
        for item in body.get(key, []) or []:
            if not isinstance(item, dict):
                continue
            f = _file_from_struct(item)
            if f:
                files.append(f)
            elif _looks_like_file_descriptor(item):
                nm = (item.get("file_name") or item.get("name") or item.get("filename")
                      or os.path.basename(item.get("path") or item.get("file_path") or "")
                      or "<unnamed attachment>")
                unreadable.append(nm)
    if files:
        notes.append(f"{len(files)} file(s) from body['files'/'attachments']")
    if unreadable:
        notes.append(f"{len(unreadable)} attached file(s) could not be decoded: {unreadable}")

    # 1b. Inline content parts.
    part_files = _files_from_content_parts(messages)
    if part_files:
        notes.append(f"{len(part_files)} file(s) from inline message parts")
        files.extend(part_files)

    # 2. Filesystem paths named in the instruction.
    path_files = _files_from_paths(instruction)
    if path_files:
        notes.append(f"{len(path_files)} file(s) from path references in the prompt")
        files.extend(path_files)

    # 3. Fallback: a conventional inputs directory.
    if not any(f.name.lower() == "template.pptx" for f in files):
        dir_files = _files_from_input_dir()
        if dir_files:
            notes.append(f"{len(dir_files)} file(s) from an inputs directory")
            files.extend(dir_files)

    files = _dedupe(files)

    # Classify template vs content.
    template: Optional[IngestedFile] = None
    for f in files:
        if f.ext == "pptx" and f.name.lower() == "template.pptx":
            template = f
            break
    if template is None:                       # heuristic fallback
        for f in files:
            if f.ext == "pptx" and _looks_like_template(f.data):
                template = f
                notes.append(f"template identified heuristically: {f.name}")
                break

    content_docs = [
        f for f in files
        if f is not template and f.ext in ALLOWED_CONTENT_EXT
    ]

    if template is not None and not content_docs:
        notes.append("WARNING: no content documents found; deck will be thin.")

    # Note: we do NOT raise here. A missing/unreadable template and unreadable attachments are both
    # reported gracefully by the caller (server.build_deck) as a plain "file not readable" reply.
    log.info("ingest: template=%s, %d content doc(s), %d unreadable; %s",
             template.name if template else None, len(content_docs), len(unreadable),
             "; ".join(notes))
    return IngestResult(instruction=instruction, template=template,
                        content_docs=content_docs[:5], notes=notes, unreadable=unreadable)

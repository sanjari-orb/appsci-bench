"""Materialize the knowledge corpus as the on-disk file tree the `shell` tool exposes.

WHY THIS EXISTS AS ITS OWN MODULE. For every other retrieval tool the corpus layout is invisible: a
search returns document excerpts through a pipeline, and how the documents happen to be stored never
reaches the agent. `shell` is the exception -- its entire surface IS a filesystem, so every byte the
agent sees comes from `ls`, `cat`, `grep` and friends against real files. Filenames, extensions and
the exact body framing are therefore agent-visible behaviour, and any drift from upstream changes what
a model's habitual command returns. The published leaderboard numbers we are trying to be comparable
to were produced against this exact tree.

BYTE-EXACT WITH UPSTREAM, DELIBERATELY. Everything here mirrors upstream's SandboxManager export
(vendored at knowledge/sandbox_manager.py): the filename sanitizer, the `# {title}` + blank line +
content body, the index filename, and the index's heading / total / id-sorted listing. The upstream
tool docstring the agent reads suggests trying the index file by name, so it has to exist and has to
look like this.

Lifted OUT of that class on purpose. The format used to be reachable only as methods on
SandboxManager, whose constructor raises unless Anthropic's sandbox-runtime is installed -- which is
why `shell` was dead in every container we have ever run (see retrieval.py). We no longer use that
runtime at all (it needs namespace privileges a task container does not have), but the byte format is
still the thing fidelity depends on, so it lives here as a plain function over documents: importable
with no runtime, no container and no privileges, which is what lets a test pin the bytes.

ONE DEFINITION ONLY. Do not reimplement this anywhere else. `shell_sidecar/Dockerfile` runs it as a
build step to bake the tree into the sidecar image, and tests assert its output; a second copy of the
format is a second thing to keep in agreement by hand.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Dict, Iterable, List

# The directory name the corpus is exported into, and the working directory every shell command runs
# in. Upstream nests it as `<sandbox>/knowledge_base`; the sidecar has no sandbox wrapper to nest
# inside, so the leaf name is what carries over -- it is visible to the agent in any output that
# prints a path, so it keeps upstream's spelling.
KB_DIR_NAME = "knowledge_base"

# Upstream's default for the shell variant, and what `alltools` therefore runs. Not a free choice:
# `.md` decides what `ls *.md` matches, and the markdown body framing decides what `head -1` returns.
DEFAULT_FILE_FORMAT = "md"


def sanitize_filename(filename: str) -> str:
    """Document id -> safe filename, byte-identical to upstream's sanitizer.

    Note it is NOT a general-purpose slugifier and should not be "improved": banking's ids contain
    parentheses (e.g. `doc_bank_accounts_bank_accounts_(general)_001`), which this maps to
    underscores, so the resulting filenames differ from our committed `.json` names. That mapping is
    part of what the agent sees.
    """
    safe = filename.replace("/", "_").replace("\\", "_").replace("..", "_")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in safe)
    return safe[:255]


def _document_body(title: str, content: str, file_format: str) -> str:
    if file_format == "json":
        raise ValueError("json export is not used by any variant we run; use md or txt")
    if file_format == "md":
        return f"# {title}\n\n{content}"
    return f"Title: {title}\n{'=' * 50}\n\n{content}"


def _index_text(documents: List[Dict[str, Any]], file_format: str) -> str:
    """The INDEX file's contents. Sorted by document id, as upstream sorts it, so the tree is
    reproducible from the corpus alone rather than depending on directory iteration order."""
    ext = file_format if file_format != "json" else "md"
    ordered = sorted(documents, key=lambda d: d.get("id", ""))
    out: List[str] = []
    if ext == "md":
        out.append("# Knowledge Base Index\n\n")
        out.append(f"**Total documents:** {len(documents)}\n\n")
        out.append("## Documents\n\n")
        for doc in ordered:
            doc_id = doc.get("id", "unknown")
            title = doc.get("title", doc_id)
            out.append(f"- **{sanitize_filename(doc_id)}.{ext}** - {title}\n")
    else:
        out.append("KNOWLEDGE BASE INDEX\n")
        out.append("=" * 50 + "\n\n")
        out.append(f"Total documents: {len(documents)}\n\n")
        out.append("Documents:\n")
        out.append("-" * 50 + "\n")
        for doc in ordered:
            doc_id = doc.get("id", "unknown")
            title = doc.get("title", doc_id)
            out.append(f"\n{sanitize_filename(doc_id)}.{ext}\n")
            out.append(f"  Title: {title}\n")
    return "".join(out)


def index_filename(file_format: str = DEFAULT_FILE_FORMAT) -> str:
    ext = file_format if file_format != "json" else "md"
    return f"INDEX.{ext}"


def export_corpus(
    documents: Iterable[Dict[str, Any]],
    out_dir: str | pathlib.Path,
    file_format: str = DEFAULT_FILE_FORMAT,
) -> Dict[str, pathlib.Path]:
    """Write `documents` into `out_dir` as the shell-visible tree; return {doc_id: path}.

    `out_dir` is created if absent and is expected to be otherwise EMPTY -- the confinement
    guarantee this tool rests on is that the shell's whole filesystem view is these documents plus
    the index, so anything else already sitting here would be readable by the agent.
    """
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    docs = list(documents)

    written: Dict[str, pathlib.Path] = {}
    for doc in docs:
        doc_id = doc.get("id", "unknown")
        title = doc.get("title", doc_id)
        content = doc.get("content", "")
        path = out / f"{sanitize_filename(doc_id)}.{file_format}"
        path.write_text(_document_body(title, content, file_format))
        written[doc_id] = path

    (out / index_filename(file_format)).write_text(_index_text(docs, file_format))
    return written


def load_documents(src_dir: str | pathlib.Path) -> List[Dict[str, Any]]:
    """Read the committed `{id, title, content}` corpus. Sorted by filename, matching corpus.py, so
    the export does not depend on filesystem iteration order."""
    base = pathlib.Path(src_dir)
    docs: List[Dict[str, Any]] = []
    for f in sorted(base.glob("*.json")):
        d = json.loads(f.read_text())
        docs.append({"id": d["id"], "title": d.get("title", ""), "content": d.get("content", "")})
    return docs


def main(argv: List[str] | None = None) -> int:
    """Build-step entry point: `python -m ..._runtime.kb_export --src DOCS --out KB`.

    Run by shell_sidecar/Dockerfile's first stage. It is build-time tooling and ships in no runtime
    container -- neither the workspace nor the sidecar has it at run time.
    """
    ap = argparse.ArgumentParser(description="Export the KB corpus as the shell-visible file tree")
    ap.add_argument("--src", required=True, help="directory of committed {id,title,content} JSON docs")
    ap.add_argument("--out", required=True, help="directory to write the exported tree into")
    ap.add_argument("--format", default=DEFAULT_FILE_FORMAT, choices=["md", "txt"])
    args = ap.parse_args(argv)

    docs = load_documents(args.src)
    if not docs:
        print(f"no documents found under {args.src}", file=sys.stderr)
        return 1
    written = export_corpus(docs, args.out, file_format=args.format)
    print(f"exported {len(written)} documents + {index_filename(args.format)} to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared checks for a task's elicitation rubric.

A rubric is only meaningful if its items could NOT have been read out of a file the agent was given.
That is the second of the six authoring rules, and the half of it that a machine can check is here:
pull the distinctive strings out of each item's fact -- the exact enum spellings, the threshold numbers,
the key names it pins -- and look for them across everything the agent can actually see.

The judgement half stays human review. No test can decide whether a fact is genuinely load-bearing, or
whether an item's phrasing sits at the elicitable granularity rather than the handed one. What this can
do is catch the item that quietly measures file-reading, which is the failure that would make the whole
metric a lie.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]

# File types worth scanning as text. A .pptx or .xlsx is a zip and is handled separately where a task
# needs it (see the slides_generator test).
_TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".csv", ".yaml", ".yml", ".sql", ".py", ".sh",
                  ".cfg", ".ini", ".toml", ".html"}

# What counts as a distinctive string in a fact. Each of these is something an item PINS -- a value the
# agent either has or does not -- as opposed to prose describing it.
_TOKEN_PATTERNS = (
    re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"),   # SCREAMING_SNAKE enum values
    re.compile(r"\b[A-Z]{4,}\b"),                       # single-word enum values, e.g. DUPLICATE
    re.compile(r"`([^`\n]{2,40})`"),                     # backticked identifiers and literals
    re.compile(r"\$\s?[\d,]+(?:\.\d+)?"),                # dollar thresholds
    re.compile(r"\b\d+\s?[-\u2013]\s?\d+\b"),               # ranges, e.g. 8-12 slides
    re.compile(r"\b\d{3,}\b"),                           # three-digit-plus numbers (amounts, limits)
)
# A one- or two-digit number is deliberately NOT a token. In a task whose fixture is megabytes of JSON,
# every small integer appears somewhere, so finding one proves nothing -- and an item built out of them
# (a nine-cell baggage allowance table, say) would read as fully leaked while being entirely sim-only.
# What such items pin is a PAIRING, which no token scan can see; the phrase scan covers them instead.

# How many consecutive words of a fact may also appear in something the agent was given. Long enough that
# ordinary domain phrasing does not collide, short enough to catch a fact written by copying a mounted
# document. This is what covers the items that pin a QUALITY rather than a value -- "every slide earns its
# place", "no overlapping elements" -- which the token scan above cannot see at all.
_MAX_SHARED_PHRASE_WORDS = 8

# Words that look distinctive but are ordinary vocabulary, so finding one in an agent-facing file says
# nothing about whether the item leaked. Facts should not use capitals for emphasis, which is what most
# of the upper-case entries here are guarding against -- reserve capitals for actual enum values.
_TOKEN_STOPWORDS = {
    # types and literals
    "true", "false", "null", "json", "string", "boolean", "integer", "number", "list", "array",
    "object",
    # emphasis and ordinary words. Facts should not use capitals for emphasis; these guard the slip.
    "and", "or", "not", "the", "any", "all", "none", "only", "must", "never", "always",
    "first", "last", "entire", "whole", "each", "every", "this", "that", "with", "without", "note",
    # `word`/`excel`/`zip` are ordinary words as often as they are product names. The file EXTENSIONS
    # (pptx, docx, xlsx, csv) stay tokens on purpose: as a set they are precisely what one item pins.
    "powerpoint", "excel", "word", "zip",
}

# A single shared token is not evidence of anything -- a task about decks says "pptx" everywhere, and an
# item whose only pinned value is its own subject matter tells the scan nothing. Such items are covered by
# the phrase scan instead, which is the better instrument for them anyway.
_MIN_TOKENS_FOR_LEAK_VERDICT = 2


def _tokens(fact: str) -> set[str]:
    found: set[str] = set()
    for pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(fact):
            token = (match.group(1) if match.groups() else match.group(0)).strip()
            token = token.strip("`.,;:()[]")
            if len(token) >= 2 and token.lower() not in _TOKEN_STOPWORDS:
                found.add(token)
    return found


def _normalise(text: str) -> str:
    """Collapse whitespace and drop dollar-sign spacing so `$ 500` and `$500` compare equal."""
    return re.sub(r"\s+", " ", text).replace("$ ", "$")


def agent_visible_text(task_id: str) -> str:
    """Everything the agent under test can read: the mounted environment tree plus the redacted spec.

    Both are real leak routes. The environment tree is bind-mounted into the agent's container and its
    shell can read whatever is there; the redacted spec is mounted in place of the real one, and its
    free-text fields (description, interface_design, resource descriptions) survive redaction.
    """
    from appsci_bench.core.spec import build_agent_spec

    parts: list[str] = []
    env = REPO / "tasks" / task_id / "environment"
    if env.is_dir():
        for path in sorted(env.rglob("*")):
            if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES:
                parts.append(path.read_text(errors="ignore"))
    spec = yaml.safe_load((REPO / "tasks" / task_id / "spec.yaml").read_text())
    parts.append(yaml.safe_dump(build_agent_spec(spec), sort_keys=False, allow_unicode=True))
    return _normalise("\n".join(parts))


def office_document_text(path: pathlib.Path) -> str:
    """The VISIBLE text of a .pptx or .docx -- the runs a reader would see, not the file's XML.

    Scanning raw OOXML instead would be worse than useless: its tag vocabulary contains "presentation",
    "elements", "notes" and "metadata", so any item naming one of those would look leaked by a template
    that says nothing about them. The format itself is explained to the agent anyway (pptx_format.md);
    what matters here is the CONTENT of a mounted example.
    """
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            xml = " ".join(archive.read(n).decode("utf8", "ignore") for n in archive.namelist()
                           if n.endswith(".xml"))
    except (zipfile.BadZipFile, OSError):
        return ""      # a Git-LFS pointer rather than the real file; nothing readable either way
    runs = re.findall(r"<(?:a|w):t(?:\s[^>]*)?>(.*?)</(?:a|w):t>", xml, re.DOTALL)
    return " ".join(runs)


def mounted_office_text(task_id: str) -> str:
    """Every mounted deck, document and spreadsheet's visible text, for a task that ships worked
    example inputs. Anything an example reveals is handed, not elicited."""
    env = REPO / "tasks" / task_id / "environment"
    if not env.is_dir():
        return ""
    return " ".join(office_document_text(p) for p in sorted(env.rglob("*"))
                    if p.suffix.lower() in {".pptx", ".docx", ".xlsx"})


def readable_tokens(fact: str, visible: str) -> set[str]:
    """Which of a fact's distinctive strings the agent could have read for itself."""
    return {t for t in _tokens(fact) if _normalise(t) in visible}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def shared_phrases(fact: str, visible: str) -> list[str]:
    """Runs of `_MAX_SHARED_PHRASE_WORDS` words the fact and the agent's own material have in common.

    A fact assembled by copying out of a mounted document is the prose equivalent of a leaked value, and
    the token scan cannot see it. Word-level rather than character-level so that reflowed line breaks and
    punctuation differences do not hide a match.
    """
    n = _MAX_SHARED_PHRASE_WORDS
    haystack = " ".join(_words(visible))
    words = _words(fact)
    hits = []
    for i in range(len(words) - n + 1):
        phrase = " ".join(words[i:i + n])
        if phrase in haystack:
            hits.append(phrase)
    return hits


def assert_rubric_is_sound(task_id: str, *, extra_visible: str = "", unpriced_ok=()) -> None:
    """The checks every task's rubric must pass.

    `extra_visible` adds task-specific agent-facing content the generic scan cannot reach (a mounted
    deck's text, say). `unpriced_ok` names the items allowed to rest only on objectives the task prices
    at zero -- see `assert_items_feed_scored_objectives`. It lives on THIS helper, the one every task
    already calls, rather than beside it: a check a task has to opt into is a check a new task does not
    get, and this one exists precisely because a weaker version of it went unnoticed for a day.

    Two complementary readings of "could the agent have read this for itself". The token scan catches an
    item whose pinned VALUES are all in a file the agent was given. The phrase scan catches an item
    written by copying that file's prose -- which is how an item pinning a quality rather than a value
    would leak, and which the token scan is blind to. Between them every item is covered by something.
    """
    from appsci_bench.core.spec import load_task_spec
    from appsci_bench.eval.elicitation_rubric import (
        MAX_ITEMS,
        load_rubric,
        validate_rubric,
        validate_rubric_document,
    )

    spec = load_task_spec(task_id)

    # 1. the file validates on its own terms
    pointer = (spec.evaluation_data or {}).get("elicitation_rubric")
    assert pointer, f"{task_id}: spec declares no elicitation_rubric"
    document = yaml.safe_load((REPO / pointer).read_text())
    assert validate_rubric_document(document) == []

    # 2. the pointer is wired, not just the file present
    rubric = load_rubric(spec)
    assert rubric is not None and rubric.task_id == task_id

    # 3. every item feeds a real objective OF THIS TASK -- what makes criticality auditable -- and a
    # PRICED one unless declared, which is the half a name comparison cannot see.
    assert validate_rubric(rubric, (spec.evaluation_data or {}).get("objectives") or []) == []
    assert_items_feed_scored_objectives(task_id, rubric, unpriced_ok=unpriced_ok)

    # 4. within the cap, ids unique
    assert 0 < len(rubric.items) <= MAX_ITEMS
    ids = [i.item_id for i in rubric.items]
    assert len(set(ids)) == len(ids)

    # 5a. no item's pinned values are all readable from something the agent was given
    visible = agent_visible_text(task_id) + _normalise(extra_visible)
    leaked = []
    for item in rubric.items:
        tokens = _tokens(item.fact)
        if (len(tokens) >= _MIN_TOKENS_FOR_LEAK_VERDICT
                and readable_tokens(item.fact, visible) == tokens):
            leaked.append((item.item_id, sorted(tokens)))
    assert leaked == [], (
        f"{task_id}: every distinctive value in these items is readable from an agent-facing file, so "
        f"they measure file-reading rather than asking: {leaked}")

    # 5b. and no item was written by copying the agent's own material
    copied = [(i.item_id, p) for i in rubric.items for p in shared_phrases(i.fact, visible)]
    assert copied == [], (
        f"{task_id}: these items share a long phrase with a file the agent can read, which means the "
        f"fact was taken from that file rather than from the stakeholder: {copied}")

    # 6. a fact is a fact, not a label wearing one's clothes. Only the crude half is mechanical --
    # whether a fact is stated completely enough to judge is what review is for.
    for item in rubric.items:
        assert len(item.fact) > len(item.item_id) * 3, f"{item.item_id}: fact is too thin to judge"


@pytest.fixture
def rubric_soundness():
    return assert_rubric_is_sound


def assert_items_feed_scored_objectives(task_id: str, rubric, *, unpriced_ok=()) -> None:
    """Every rubric item names objectives the task computes -- and any item resting ONLY on objectives
    the task has priced at zero must be named in `unpriced_ok`.

    The name check alone used to be the whole test, and it went quiet exactly when it was needed: the
    airline task later set `policy_compliance` to weight 0 so that its final score would be the one
    figure comparable to a published tau2 number. Five items pointed only at that objective from then
    on -- still a scored, reported objective, but one whose absence can no longer move the task score,
    which is the question the rubric's first authoring rule asks. Comparing names could not see the
    change, so nothing failed.

    An unpriced item is not automatically wrong: an objective can be computed and reported while
    deliberately left out of the weighted score. What is wrong is arriving there silently. Naming each
    one here forces the reason to be written down, and makes a NEW item that lands on an unpriced
    objective fail until someone decides that is what they meant.
    """
    from appsci_bench.core.spec import load_task_spec

    objectives = (load_task_spec(task_id).evaluation_data or {}).get("objectives") or []
    weights = {o["name"]: float(o.get("weight") or 0.0) for o in objectives}
    # A task that leaves EVERY weight at zero is not a task with no priced objectives: the aggregator
    # falls back to a plain unweighted mean when the weights sum to zero (aggregate.py's
    # `_weighted_mean`), so all of them move the score equally. Reading that as "nothing is priced"
    # would flag every item on such a task and tell the author to fix something already correct.
    if weights and not any(weights.values()):
        weights = dict.fromkeys(weights, 1.0)
    for item in rubric.items:
        assert item.objectives, f"{item.item_id} feeds no objective"
        unknown = set(item.objectives) - set(weights)
        assert not unknown, f"{item.item_id} names objectives this task does not score: {sorted(unknown)}"

    unpriced = sorted(i.item_id for i in rubric.items
                      if all(weights[o] == 0.0 for o in i.objectives))
    assert unpriced == sorted(unpriced_ok), (
        f"{task_id}: items resting only on zero-weight objectives changed.\n"
        f"  found:    {unpriced}\n  declared: {sorted(unpriced_ok)}\n"
        "Either give the item an objective the task actually prices, or add it to this task's "
        "declared list with the reason it measures something the task reports but does not score.")

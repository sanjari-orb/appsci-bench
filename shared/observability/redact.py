"""Scrub credentials out of captured LLM I/O before it is written to disk.

**Why this exists.** A model-call transcript is whatever the agent said and saw. When the agent runs
a shell command whose output happens to include its own environment -- an `env` dump, a process
listing, a config echo -- that output becomes a message, and the capture in :mod:`model_io` records
it verbatim. A real run leaked a live gateway JWT into `model_calls-*.jsonl` this way, 180 times
across 4 runs, and because runs are mirrored to S3 the secret left the machine.

So the fix cannot be "stop logging field X" -- the secret arrives as arbitrary agent output. It has
to be a redaction pass, and it belongs **at write time** rather than at upload time:

* the secret never touches disk, so a local run dir is not a liability either;
* the upload stays a pure byte copy, which keeps the mirror byte-identical to the local run and
  keeps the size-based skip-if-unchanged check meaningful. Redacting during upload would break both.

**What counts as a secret.** Only values this process already holds in its own environment under a
credential-ish name, and only if they are long enough to be a real token. That makes redaction exact
-- no heuristics against the message text, so no false positives mangling legitimate content.

Disable with ``OBSERVABILITY_REDACT=0`` if you are debugging a capture problem and accept the risk.
"""
from __future__ import annotations

import os
import re
from typing import Any

#: Env var names whose values are treated as secrets. Deliberately broad -- a missed name means a
#: leaked credential, an extra one costs nothing but a redacted string in a transcript.
_SECRET_NAME_RE = re.compile(
    r"(API_?KEY|_KEY$|^KEY$|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE
)

#: Shorter values are too likely to be a flag, a model name or a placeholder to redact safely --
#: replacing a common short string throughout every transcript would do more harm than good.
_MIN_SECRET_LEN = 20

#: Values that match a credential-ish name but are obviously not live credentials.
_PLACEHOLDERS = frozenset({"changeme", "placeholder", "redacted", "none", "null", "unset", "dummy"})

_cache: dict[str, str] | None = None


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip("<>[]{}\"'").lower()
    return v in _PLACEHOLDERS or set(v) <= {".", "*", "x", "-", "_"}


def secret_values(env: dict[str, str] | None = None) -> dict[str, str]:
    """Map ``secret value -> label`` for every credential-ish env var worth redacting.

    Keyed by *value*, so one credential exposed under several names (a gateway token reused as
    ``ANTHROPIC_AUTH_TOKEN`` / ``MODEL_API_KEY`` / ``APPSCI_AGENT_API_KEY``, which is exactly what
    the real leak looked like) is redacted once under a single label.
    """
    global _cache
    if env is None and _cache is not None:
        return _cache
    source = env if env is not None else os.environ
    found: dict[str, list[str]] = {}
    for name, value in source.items():
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        if not _SECRET_NAME_RE.search(name) or _is_placeholder(value):
            continue
        found.setdefault(value, []).append(name)
    result = {value: f"[REDACTED:{sorted(names)[0]}]" for value, names in found.items()}
    if env is None:
        _cache = result
    return result


def reset_cache() -> None:
    """Forget the memoized environment scan (tests, or a process that rotates its own env)."""
    global _cache
    _cache = None


def enabled() -> bool:
    """Whether redaction is active. On unless ``OBSERVABILITY_REDACT`` is falsy."""
    return (os.getenv("OBSERVABILITY_REDACT") or "1").strip().lower() not in ("0", "false", "no")


def scrub(obj: Any, secrets: dict[str, str] | None = None) -> Any:
    """Return ``obj`` with every known secret value replaced by a label.

    Walks strings, dicts and lists; leaves other types alone. Returns the input unchanged when
    redaction is disabled or there is nothing to redact, so the hot path costs one dict lookup.
    """
    if secrets is None:
        if not enabled():
            return obj
        secrets = secret_values()
    if not secrets:
        return obj

    if isinstance(obj, str):
        # `in` before `replace` because the overwhelmingly common case is no match, and scanning is
        # much cheaper than building a new string. Transcripts can be megabytes.
        for value, label in secrets.items():
            if value in obj:
                obj = obj.replace(value, label)
        return obj
    if isinstance(obj, dict):
        return {k: scrub(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, secrets) for v in obj]
    return obj

"""Minimal Anthropic Messages client for the deployed artifact.

Stdlib only -- the deployer injects ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN (a gateway) or a
first-party ANTHROPIC_API_KEY, and nothing else is guaranteed to be present.

One deliberate constraint: the gateway in front of us mistranslates a turn carrying MORE THAN ONE
tool_use block, so we ask for one tool call at a time and still cope if we get several.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "claude-opus-4-8"

# The gateway enforces a per-user requests-per-minute budget, and eval runs this artifact at high
# concurrency. Capping our own in-flight calls keeps a burst from turning into 429s that would
# silently demote every answer to the rule-based fallback.
_gate = threading.Semaphore(int(os.environ.get("ARTIFACT_LLM_CONCURRENCY", "4")))


class LLMUnavailable(RuntimeError):
    pass


def _base_url() -> str | None:
    for var in ("ANTHROPIC_BASE_URL", "APPSCI_AGENT_BASE_URL", "MODEL_BASE_URL"):
        if os.environ.get(var):
            return os.environ[var].rstrip("/")
    return None


def _auth_headers() -> dict[str, str]:
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return {"authorization": f"Bearer {os.environ['ANTHROPIC_AUTH_TOKEN']}"}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"x-api-key": os.environ["ANTHROPIC_API_KEY"]}
    for var in ("APPSCI_AGENT_API_KEY", "MODEL_API_KEY"):
        if os.environ.get(var):
            return {"authorization": f"Bearer {os.environ[var]}"}
    return {}


def _custom_headers() -> dict[str, str]:
    """Gateway identity headers, if the deploy env carried them (optional for a personal token)."""
    out: dict[str, str] = {}
    raw = (os.environ.get("ANTHROPIC_CUSTOM_HEADERS")
           or os.environ.get("APPSCI_AGENT_CUSTOM_HEADERS")
           or os.environ.get("MODEL_CUSTOM_HEADERS") or "")
    for part in raw.replace("|", "\n").splitlines():
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def model_name() -> str:
    raw = os.environ.get("ARTIFACT_MODEL") or os.environ.get("MODEL_NAME") or DEFAULT_MODEL
    return raw.split("/", 1)[1] if "/" in raw else raw


def available() -> bool:
    return bool(_base_url() or os.environ.get("ANTHROPIC_API_KEY")) and bool(_auth_headers())


def complete(messages: list[dict], system: str, tools: list[dict] | None = None,
             max_tokens: int = 4096, timeout: int = 180, attempts: int = 6) -> dict:
    """One Messages API round-trip, with retries on transient gateway failures."""
    if not available():
        raise LLMUnavailable("no model credentials in the environment")
    base = _base_url() or "https://api.anthropic.com"
    url = f"{base}/v1/messages"
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
               **_auth_headers(), **_custom_headers()}
    payload: dict = {"model": model_name(), "max_tokens": max_tokens,
                     "system": system, "messages": messages}
    if tools:
        payload["tools"] = tools
        # one tool call per turn: the gateway cannot serialise a parallel tool turn
        payload["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

    last = ""
    for attempt in range(1, attempts + 1):
        wait = None
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
            with _gate:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()[:500]
            last = f"HTTP {exc.code}: {body}"
            # an unsupported tool_choice field is permanent -- retry once without it
            if exc.code == 400 and "disable_parallel_tool_use" in body and "tool_choice" in payload:
                payload.pop("tool_choice")
                continue
            if exc.code in (400, 401, 403, 404):
                raise LLMUnavailable(last) from exc
            if exc.code == 429:
                try:  # the gateway tells us how long to wait; believe it over our own backoff
                    wait = float((exc.headers or {}).get("retry-after") or 0) or None
                except (TypeError, ValueError):
                    wait = None
        except Exception as exc:  # noqa: BLE001 -- timeouts, resets, DNS
            last = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(wait if wait else min(2 ** attempt, 20) + random.uniform(0, 1.5))
    raise LLMUnavailable(f"model call failed after {attempts} attempts -- {last}")


def probe() -> dict:
    """Cheap reachability check for /selftest."""
    try:
        data = complete([{"role": "user", "content": "Reply with the single word: ready"}],
                        system="You are a health check.", max_tokens=16, timeout=45, attempts=2)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return {"ok": True, "model": model_name(), "reply": text.strip()[:40]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "model": model_name(), "error": str(exc)[:300]}

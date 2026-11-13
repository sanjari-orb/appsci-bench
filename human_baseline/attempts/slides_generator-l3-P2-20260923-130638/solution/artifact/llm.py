"""Thin wrapper over the Anthropic SDK for Claude Opus 4.8 (the agent's engine).

Design notes:
- Zero-arg `Anthropic()` reads `ANTHROPIC_API_KEY` from the environment — the deployer injects it at
  runtime, so nothing is hardcoded.
- `claude-opus-4-8` is vision-capable: we pass base64 image blocks and PDF `document` blocks.
- Long generations stream and use `.get_final_message()` (avoids HTTP timeouts on big `max_tokens`).
- Structured JSON comes back via tool use (prefill is removed on 4.8). We instruct the model to call
  a single "emit" tool whose `input_schema` is our schema, then read the tool_use block's input; if
  the model answers in text instead, we fall back to parsing JSON out of the text.
- All model-facing params (`thinking`, `output_config`) are passed if the installed SDK accepts them
  and dropped gracefully otherwise, so the wrapper survives minor SDK-version drift.
- The whole engine sits behind this one class so the pipeline stages can be unit-tested offline with a
  fake client (no API key needed at build time).
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger("slidesgen.llm")

MODEL = "claude-opus-4-8"


class LLMUnavailable(RuntimeError):
    """Raised when a model call is attempted without usable credentials/SDK."""


# --------------------------------------------------------------------------- content-block helpers


def text_block(text: str, cache: bool = False) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def image_block(data: bytes, media_type: str = "image/png") -> Dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def pdf_block(data: bytes) -> Dict[str, Any]:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _strip_json(text: str) -> str:
    """Best-effort: pull a JSON object out of a text answer (handles ```json fences)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start : end + 1]
    return t


class LLMClient:
    def __init__(self, raw_client: Optional[Any] = None):
        self._client = raw_client
        if raw_client is None:
            try:
                import anthropic  # noqa: F401

                if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
                    from anthropic import Anthropic

                    # Generous timeout: deck generation involves large multimodal prompts.
                    self._client = Anthropic(max_retries=3, timeout=600.0)
            except Exception as exc:  # pragma: no cover - import/creds problems
                log.warning("Anthropic client unavailable: %s", exc)
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------ low-level create

    def _final_message(self, **kwargs: Any) -> Any:
        if not self.available:
            raise LLMUnavailable("No Anthropic client (missing SDK or ANTHROPIC_API_KEY).")
        # Try with the modern params; drop any the installed SDK rejects and retry once.
        try:
            with self._client.messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        except TypeError as exc:
            log.warning("SDK rejected kwargs (%s); retrying without thinking/output_config.", exc)
            for k in ("thinking", "output_config"):
                kwargs.pop(k, None)
            with self._client.messages.stream(**kwargs) as stream:
                return stream.get_final_message()

    # ------------------------------------------------------------------ public API

    def complete_text(
        self,
        *,
        system: str,
        content: List[Dict[str, Any]],
        effort: str = "high",
        max_tokens: int = 16000,
    ) -> str:
        msg = self._final_message(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": content}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()

    def complete_json(
        self,
        *,
        system: str,
        content: List[Dict[str, Any]],
        schema: Dict[str, Any],
        tool_name: str = "emit",
        tool_description: str = "Return the structured result.",
        effort: str = "high",
        max_tokens: int = 16000,
        strict: bool = False,
    ) -> Dict[str, Any]:
        """Get a schema-shaped dict back via a single tool the model is told to call."""
        tool = {
            "name": tool_name,
            "description": tool_description,
            "input_schema": schema,
        }
        if strict:
            tool["strict"] = True
        instruction = text_block(
            f"\n\nCall the `{tool_name}` tool exactly once with the complete result. "
            f"Do not answer in prose."
        )
        msg = self._final_message(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            tools=[tool],
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": content + [instruction]}],
        )
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return dict(block.input)
        # Fallback: the model answered in text — try to parse JSON out of it.
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        if text.strip():
            try:
                return json.loads(_strip_json(text))
            except Exception as exc:
                raise LLMUnavailable(f"Model did not return usable structured output: {exc}")
        raise LLMUnavailable("Model returned neither a tool call nor text.")

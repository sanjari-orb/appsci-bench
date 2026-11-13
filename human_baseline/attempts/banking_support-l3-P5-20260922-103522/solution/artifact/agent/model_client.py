"""Thin wrapper over the Anthropic-compatible Messages endpoint.

Reads the base URL and key from Config (MODEL_BASE_URL / MODEL_API_KEY, with fallbacks). Uses the
official ``anthropic`` SDK pointed at the custom base URL; if the SDK cannot be constructed against
this endpoint we surface the error rather than silently guessing a transport.

Kept deliberately minimal: one ``create`` call that forwards system prompt, messages, and tool
schemas and returns the raw SDK response. The turn loop owns the tool-use control flow.
"""
from __future__ import annotations

from typing import Any

import anthropic

from .config import Config

# Generous ceiling; the loop bounds iterations, and turns are short natural-language replies.
MAX_TOKENS = 4096


class ModelClient:
    def __init__(self, config: Config) -> None:
        self._model = config.model_name
        self._client = anthropic.Anthropic(
            base_url=config.model_base_url,
            api_key=config.model_api_key,
        )

    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> anthropic.types.Message:
        return self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            tools=tools,
        )

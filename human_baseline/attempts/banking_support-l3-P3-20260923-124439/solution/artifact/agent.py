"""Server entry point for the Rho-Bank agent core.

Exposes ``run_agent(messages) -> str``, which ``server.py`` imports lazily from ``/respond``. Creating
this module is exactly what flips ``server._load_agent()`` from None to live, so /respond stops
returning the honest 503 and starts producing grounded answers.

It wires together interfaces confirmed to exist -- nothing here is guessed:
  * AnthropicMessagesClient.from_config()  -- the LLM (claude-opus-4-8 via Anthropic Messages),
  * ToolClient(url, token)                 -- the banking tool API transport,
  * prompts.SYSTEM_PROMPT                  -- the elicited policy,
  * tool_specs.AGENT_TOOLS                 -- confirmed retrieval + BASE + discovery ToolSpecs.

Construction reads runtime env, so it is done lazily INSIDE run_agent, never at import time. That keeps
``import agent`` side-effect-free: a missing or invalid runtime env surfaces as an honest per-request
error (server.py turns any raise into an HTTP 500 error envelope, never a fabricated answer) rather
than an import failure that server._load_agent would misread. The httpx-backed clients are closed after
each request.
"""
from __future__ import annotations

from typing import Any, List

import config
import prompts
import tool_specs
from agent_core import AgentLoop
from llm_client import AnthropicMessagesClient
from tools import ToolClient


def run_agent(messages: List[Any]) -> str:
    """Handle one /respond turn: build the agent from runtime config, run the loop, return the text.

    Raises on missing runtime configuration (banking or Anthropic env); server.py converts any
    exception into an honest error response instead of a fabricated assistant turn.
    """
    url, token = config.require_banking_tools()
    with AnthropicMessagesClient.from_config() as llm, ToolClient(url, token) as tool_client:
        loop = AgentLoop(llm, tool_client, tool_specs.AGENT_TOOLS, prompts.SYSTEM_PROMPT)
        return loop.run(messages)

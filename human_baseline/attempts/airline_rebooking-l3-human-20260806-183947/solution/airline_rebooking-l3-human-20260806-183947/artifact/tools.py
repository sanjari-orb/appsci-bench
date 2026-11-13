"""Tool definitions for the LLM, built from the reference schema.

`tools_reference.json` is the authoritative list of the 14 airline tools, already expressed as
JSON Schema per tool, so the translation to Anthropic's `tools` parameter is mechanical. Its
`tool_type` field (WRITE / READ / GENERIC) also tells us which calls the policy guard must judge,
so no hand-maintained list of write tools is needed.

Two descriptions are enriched here rather than by editing the JSON, because the schema is silent
on details the model otherwise gets wrong -- and getting them wrong means quoting the customer a
price that is not what will be charged.
"""
from __future__ import annotations

import functools
import json
import pathlib

REFERENCE_PATH = pathlib.Path(__file__).resolve().parent / "tools_reference.json"

# Appended to the reference description. Both facts were confirmed by executing the real tool.
DESCRIPTION_NOTES = {
    "update_reservation_flights": (
        " IMPORTANT: `flights` must be the COMPLETE new list of segments for the reservation, not "
        "just the ones being changed. A segment keeps its originally-paid price only if its flight "
        "number and date are unchanged AND the cabin you pass equals the reservation's current "
        "cabin; otherwise it is re-priced at today's fare. So a cabin change re-prices EVERY "
        "segment at current prices -- it is not the difference between cabins on the fare "
        "originally paid."
    ),
    "cancel_reservation": (
        " IMPORTANT: this performs no eligibility checking of its own. Confirm the cancellation is "
        "permitted before calling it."
    ),
}


@functools.lru_cache(maxsize=1)
def _reference() -> list[dict]:
    """The parsed tool reference (cached; the file does not change at runtime)."""
    return json.loads(REFERENCE_PATH.read_text())


def llm_tools() -> list[dict]:
    """The 14 tools in Anthropic `tools` format, with the clarifying notes applied."""
    tools = []
    for entry in _reference():
        function = entry["function"]
        name = function["name"]
        tools.append({
            "name": name,
            "description": function.get("description", "") + DESCRIPTION_NOTES.get(name, ""),
            "input_schema": function["parameters"],
        })
    return tools


def write_tool_names() -> set[str]:
    """Names of the tools that mutate the database, per the reference's own `tool_type`."""
    return {e["function"]["name"] for e in _reference() if e.get("tool_type") == "WRITE"}

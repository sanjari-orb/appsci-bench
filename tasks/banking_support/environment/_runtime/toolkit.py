"""Minimal ToolKit framework (subset of tau2.environment.toolkit).

Provides exactly what the vendored domain tools.py uses: the @is_tool / @is_discoverable_tool
decorators, the ToolType enum, the ToolKitType metaclass (collects tool methods across the MRO),
and a ToolKitBase exposing tool execution + the discoverable-tool registry the meta-tools
(unlock_/call_/give_discoverable_*) rely on.

Dropped vs upstream: Tool/as_tool/get_tools/ToolSignature (LLM tool-schema generation -- the
artifact builds its own schemas; the environment only EXECUTES tools) and the pydantic-hash
helpers (the environment computes its own canonical state hash).
"""
from __future__ import annotations

from enum import Enum
from typing import Callable, Dict, Optional

TOOL_ATTR = "__tool__"
TOOL_TYPE_ATTR = "__tool_type__"
MUTATES_STATE_ATTR = "__mutates_state__"
DISCOVERABLE_ATTR = "__discoverable__"


class ToolKitType(type):
    """Metaclass that collects @is_tool-marked methods into `_func_tools` across the MRO."""

    def __init__(cls, name, bases, attrs):
        func_tools = {}
        for attr_name, method in attrs.items():
            if isinstance(method, property):
                method = method.fget
            if hasattr(method, TOOL_ATTR):
                func_tools[attr_name] = method

        @property
        def _func_tools(self) -> Dict[str, Callable]:
            all_func_tools = func_tools.copy()
            try:
                all_func_tools.update(super(cls, self)._func_tools)
            except AttributeError:
                pass
            return all_func_tools

        cls._func_tools = _func_tools


class ToolType(str, Enum):
    READ = "read"
    WRITE = "write"
    THINK = "think"
    GENERIC = "generic"


def _mark(func, tool_type, mutates_state, discoverable):
    if mutates_state is None:
        mutates_state = tool_type == ToolType.WRITE
    setattr(func, TOOL_ATTR, True)
    setattr(func, TOOL_TYPE_ATTR, tool_type)
    setattr(func, MUTATES_STATE_ATTR, mutates_state)
    if discoverable:
        setattr(func, DISCOVERABLE_ATTR, True)
    return func


def is_tool(tool_type: ToolType = ToolType.READ, mutates_state: Optional[bool] = None):
    """Mark a method as a (non-discoverable) tool. `mutates_state` defaults to True for WRITE."""
    def decorator(func):
        return _mark(func, tool_type, mutates_state, discoverable=False)
    return decorator


def is_discoverable_tool(tool_type: ToolType = ToolType.READ, mutates_state: Optional[bool] = None):
    """Mark a method as a discoverable tool (not surfaced by default; unlocked via the KB)."""
    def decorator(func):
        return _mark(func, tool_type, mutates_state, discoverable=True)
    return decorator


class ToolKitBase(metaclass=ToolKitType):
    """Base for KnowledgeTools / KnowledgeUserTools: holds the db and exposes tool execution."""

    def __init__(self, db=None):
        self.db = db

    @property
    def tools(self) -> Dict[str, Callable]:
        return {name: getattr(self, name) for name in self._func_tools.keys()}

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self.tools

    def use_tool(self, tool_name: str, **kwargs) -> str:
        if tool_name not in self.tools:
            raise ValueError(f"Tool '{tool_name}' not found.")
        return self.tools[tool_name](**kwargs)

    def is_discoverable(self, tool_name: str) -> bool:
        if tool_name not in self.tools:
            return False
        return getattr(self.tools[tool_name], DISCOVERABLE_ATTR, False)

    def get_discoverable_tools(self) -> Dict[str, Callable]:
        return {name: tool for name, tool in self.tools.items()
                if getattr(tool, DISCOVERABLE_ATTR, False)}

    def has_discoverable_tool(self, tool_name: str) -> bool:
        return tool_name in self.get_discoverable_tools()

    def tool_type(self, tool_name: str) -> ToolType:
        return getattr(self.tools[tool_name], TOOL_TYPE_ATTR)

    def tool_mutates_state(self, tool_name: str) -> bool:
        return getattr(self.tools[tool_name], MUTATES_STATE_ATTR, True)

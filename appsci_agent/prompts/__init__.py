"""Prompt templates and rendering for the Applied AI Scientist agent.

This subpackage contains Jinja2 templates and the PromptBuilder class
for generating system prompts from task context.
"""
from .builder import PromptBuilder, build_system_prompt

__all__ = [
    "PromptBuilder",
    "build_system_prompt",
]

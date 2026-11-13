"""Tracer web server for BUILD phase observability.

Provides a real-time dashboard for viewing events from benchmark runs.
"""
from .server import TracerServer

__all__ = ["TracerServer"]

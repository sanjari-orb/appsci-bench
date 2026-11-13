"""Shared fixtures for the scripts/ tool tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_elicitation_retry_delay(monkeypatch):
    """Zero the elicitation judge's between-attempt waits -- see the eval conftest for why they exist."""
    monkeypatch.setattr("appsci_bench.eval.elicitation._RETRY_DELAYS_S", (0.0, 0.0))

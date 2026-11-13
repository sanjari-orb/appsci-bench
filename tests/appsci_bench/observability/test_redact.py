"""Credential redaction in captured LLM I/O.

Regression cover for a real leak: a live gateway JWT reached `model_calls-*.jsonl` 180 times across
4 runs because the agent ran a shell command that echoed its own environment, and those runs were
then mirrored to S3. The secret arrives as *agent output*, so the defence is a redaction pass at
write time rather than a change to any particular field.
"""
from __future__ import annotations

import json

import pytest

from shared.observability import model_io, redact

JWT = "tok-" + "a1b2c3d4e5" * 4


@pytest.fixture(autouse=True)
def _clear_cache():
    redact.reset_cache()
    yield
    redact.reset_cache()


def test_detects_credential_env_vars_by_name():
    s = redact.secret_values({
        "APPSCI_AGENT_API_KEY": JWT,
        "MODEL_BASE_URL": "https://gateway.example.com/v1",   # not a credential name
        "MODEL_NAME": "anthropic/claude-opus-4-8",            # not a credential name
    })
    assert list(s.values()) == ["[REDACTED:APPSCI_AGENT_API_KEY]"]


def test_one_credential_under_several_names_is_labelled_once():
    """The real leak: one gateway token exported as three different variables."""
    s = redact.secret_values({
        "ANTHROPIC_AUTH_TOKEN": JWT, "MODEL_API_KEY": JWT, "APPSCI_AGENT_API_KEY": JWT,
    })
    assert s == {JWT: "[REDACTED:ANTHROPIC_AUTH_TOKEN]"}


@pytest.mark.parametrize("name,value", [
    ("MODEL_API_KEY", "short"),                  # too short to be a token
    ("API_KEY", "changeme"),                     # obvious placeholder
    ("SECRET_TOKEN", "xxxxxxxxxxxxxxxxxxxxxxx"),  # obvious placeholder
    ("MODEL_NAME", "anthropic/claude-opus-4-8"),  # not credential-ish
])
def test_does_not_redact_non_secrets(name, value):
    assert redact.secret_values({name: value}) == {}


def test_scrub_walks_nested_structures():
    s = redact.secret_values({"MODEL_API_KEY": JWT})
    out = redact.scrub({
        "messages": [{"role": "user", "content": f"$ env\nMODEL_API_KEY={JWT}\nHOME=/root"}],
        "tool_calls": [{"arguments": {"cmd": f"curl -H 'x: {JWT}'"}}],
        "usage": {"total_tokens": 10},
    }, s)
    blob = json.dumps(out)
    assert JWT not in blob
    assert blob.count("[REDACTED:MODEL_API_KEY]") == 2
    assert out["usage"]["total_tokens"] == 10      # non-strings untouched
    assert "HOME=/root" in out["messages"][0]["content"]   # surrounding text preserved


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_REDACT", "0")
    monkeypatch.setenv("MODEL_API_KEY", JWT)
    redact.reset_cache()
    assert redact.scrub(f"key={JWT}") == f"key={JWT}"


# ---------------------------------------------------------------------------
# The write path -- what actually stops the leak
# ---------------------------------------------------------------------------


def test_model_call_record_is_scrubbed_on_disk(tmp_path, monkeypatch):
    """End to end: a captured shell-output message containing the token must not reach disk."""
    monkeypatch.setenv("APPSCI_AGENT_API_KEY", JWT)
    redact.reset_cache()

    from shared.observability.events import Source
    from shared.observability.publisher import init_publisher, get_publisher

    init_publisher(runs_dir=str(tmp_path), trace_id="t1", source=Source.APPSCI_AGENT, task_id="t")
    try:
        # Exactly the real shape: the agent ran `env` and the output became a message.
        mc = model_io.model_call_start(
            [{"role": "user", "content": f"$ env\nAPPSCI_AGENT_API_KEY={JWT}\n"}],
            model="m", caller="mini_swe",
        )
        model_io.model_call_end(mc, content=f"the key is {JWT}", tool_calls=[])
    finally:
        pub = get_publisher()
        if pub is not None:
            pub.close()

    store = tmp_path / "t1" / "model_calls-appsci_agent.jsonl"
    raw = store.read_text()
    assert JWT not in raw, "credential reached the model-calls store"
    assert "[REDACTED:APPSCI_AGENT_API_KEY]" in raw

    events = (tmp_path / "t1" / "events-appsci_agent.jsonl").read_text()
    assert JWT not in events, "credential reached the event log via a preview"

    # The record is still usable -- redaction must not corrupt the structure.
    rec = json.loads([ln for ln in raw.splitlines() if ln.strip()][0])
    assert rec["caller"] == "mini_swe" and rec["messages"][0]["role"] == "user"

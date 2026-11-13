"""A failed model call must reach the run log and the event stream, and still raise unchanged."""
import logging

import pytest

from appsci_bench.common import model_config as mc


class _Boom(Exception):
    """Stands in for a provider error carrying an HTTP status, e.g. a gateway 429."""

    status_code = 429


def test_a_failing_completion_is_logged_and_emitted_and_still_raises(monkeypatch, caplog):
    emitted = []

    def _fake_emit(**kwargs):
        emitted.append(kwargs)
        return "span"

    monkeypatch.setattr(mc.litellm, "completion",
                        lambda **kw: (_ for _ in ()).throw(_Boom("rate limit exceeded (limit: 300 req/min)")))
    monkeypatch.setattr("shared.observability.publisher.emit", _fake_emit)

    cfg = mc.ModelConfig(model="gateway/some-model", api_key="k", base_url="http://gw")
    with caplog.at_level(logging.WARNING), pytest.raises(_Boom):
        mc.complete([{"role": "user", "content": "hi"}], cfg, caller="user_sim")

    assert "model call failed" in caplog.text
    assert "429" in caplog.text
    assert len(emitted) == 1
    payload = emitted[0]["payload"]
    assert payload["error_type"] == "_Boom"
    assert payload["status_code"] == "429"
    assert "300 req/min" in payload["error"]
    assert payload["caller"] == "user_sim"


def test_reporting_that_itself_breaks_does_not_replace_the_real_error(monkeypatch):
    """The caller's exception is the useful one; a broken publisher must not shadow it."""
    def _bad_emit(**kwargs):
        raise RuntimeError("publisher is wedged")

    monkeypatch.setattr(mc.litellm, "completion",
                        lambda **kw: (_ for _ in ()).throw(_Boom("upstream is down")))
    monkeypatch.setattr("shared.observability.publisher.emit", _bad_emit)

    cfg = mc.ModelConfig(model="gateway/some-model", api_key="k", base_url="http://gw")
    with pytest.raises(_Boom, match="upstream is down"):
        mc.complete([{"role": "user", "content": "hi"}], cfg)

"""Structured JSON logging with a per-request correlation id.

Every log line carries the conversation's correlation id, so a single customer interaction can be
reconstructed end to end from the container logs -- which is how a wrong outcome gets traced back to
the layer that produced it (policy verdict vs. tool call vs. LLM turn).
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

# Attributes present on every LogRecord; anything else a caller passes via `extra=` is treated as
# structured context and emitted inside the JSON payload.
_STANDARD = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName"}


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


def correlation_id() -> str:
    return _correlation_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "cid": correlation_id(),
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn's own access log is noisy and already covered by our /respond logging.
    logging.getLogger("uvicorn.access").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

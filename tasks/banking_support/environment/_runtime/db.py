"""Minimal pydantic DB base (subset of tau2.environment.db.DB).

Only what the vendored data_model.TransactionalDB needs: a pydantic base that forbids extra
fields (mirrors tau2's BaseModelNoExtra) and a load() classmethod. Hashing is done by the
environment wrapper (canonical json over model_dump), not here.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from pydantic import BaseModel, ConfigDict


class DB(BaseModel):
    """Base class for the transactional database (extra fields forbidden, like tau2)."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def load(cls, path: str | pathlib.Path) -> "DB":
        return cls.model_validate(json.loads(pathlib.Path(path).read_text()))

    def get_statistics(self) -> dict[str, Any]:
        return {}

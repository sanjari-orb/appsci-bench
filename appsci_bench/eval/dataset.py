"""Load + validate a task's evaluation Dataset.

The dataset's run controls (`type`, `max_turns` or `max_steps`, `repeat`) live in the task spec's
`evaluation_data` block; the samples themselves come from the `dataset` directory pointer (one
subdirectory per data_point_id, each holding a `sample.json` + any files it references by
`file_path`) or an inline `data` list. `parse_dataset`/`_build_dataset` are the pure core (unit
tested); `load_dataset` wires it to a TaskSpec / directory. See design: Datasets.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Iterable

from .datatypes import Dataset, EvalSample, EvalTypeError

_VALID_TYPES = ("single_turn", "multi_turn")


def parse_dataset(config: dict, samples: list[dict]) -> Dataset:
    """Build + validate a Dataset from its config block and raw sample dicts (no `file_path`
    support -- these dicts have no natural base directory to resolve a relative path against; use
    `load_dataset`'s directory-based path for samples with on-disk files).

    config: {type, max_turns? OR max_steps?, repeat?}. samples: list of EvalSample dicts.
    Raises EvalTypeError on any structural problem (surfaced before any artifact query).
    """
    return _build_dataset(config, (_parse_sample(raw, i) for i, raw in enumerate(samples)))


def _parse_sample(raw: dict, ref: Any, *, base_dir: pathlib.Path | None = None) -> EvalSample:
    try:
        return EvalSample.from_dict(raw, base_dir=base_dir)
    except KeyError as e:
        raise EvalTypeError(f"sample {ref!r} missing required field {e}") from e


def _build_dataset(config: dict, eval_samples: Iterable[EvalSample]) -> Dataset:
    """Shared validation core: type/max_turns/repeat + dedup + turn-shape checks over already-
    parsed EvalSamples, regardless of where they came from (inline dicts or a sample directory)."""
    dtype = config.get("type")
    if dtype not in _VALID_TYPES:
        raise EvalTypeError(f"dataset 'type' must be one of {_VALID_TYPES}, got {dtype!r}")

    max_steps = config.get("max_steps")
    if max_steps is not None:
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
            raise EvalTypeError("dataset 'max_steps' must be an integer >= 1")
        if dtype != "multi_turn":
            raise EvalTypeError("dataset 'max_steps' requires a multi_turn dataset")
        if "max_turns" in config:
            raise EvalTypeError("choose 'max_steps' or 'max_turns', not both")
    max_turns = None if max_steps is not None else config.get("max_turns", 10)
    repeat = config.get("repeat", 1)
    if max_steps is None and (not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1):
        raise EvalTypeError(f"dataset 'max_turns' must be an integer >= 1, got {max_turns!r}")
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
        raise EvalTypeError(f"dataset 'repeat' must be an integer >= 1, got {repeat!r}")

    parsed: list[EvalSample] = []
    seen: set[str] = set()
    for sample in eval_samples:
        if sample.data_point_id in seen:
            raise EvalTypeError(f"duplicate data_point_id {sample.data_point_id!r}")
        seen.add(sample.data_point_id)
        _check_turn_shape(dtype, sample)
        parsed.append(sample)

    return Dataset(type=dtype, data=parsed, max_turns=max_turns, repeat=repeat, max_steps=max_steps)


def _check_turn_shape(dtype: str, s: EvalSample) -> None:
    """single_turn samples carry single_turn_input (not multi_turn_input); multi_turn the reverse."""
    if dtype == "single_turn":
        if s.single_turn_input is None:
            raise EvalTypeError(f"{s.data_point_id!r}: single_turn dataset needs 'single_turn_input'")
        if s.multi_turn_input is not None:
            raise EvalTypeError(f"{s.data_point_id!r}: single_turn sample must not set 'multi_turn_input'")
    else:  # multi_turn
        if s.multi_turn_input is None:
            raise EvalTypeError(f"{s.data_point_id!r}: multi_turn dataset needs 'multi_turn_input'")
        if s.single_turn_input is not None:
            raise EvalTypeError(f"{s.data_point_id!r}: multi_turn sample must not set 'single_turn_input'")


def _read_sample_dirs(dataset_dir: pathlib.Path) -> list[EvalSample]:
    """Read EvalSamples from a directory of per-data_point_id subdirectories, each holding a
    `sample.json` (the EvalSample dict) plus any files it references by `file_path`, relative to
    that same subdirectory. Files are resolved (existence-checked) but NOT read here -- their bytes
    are read lazily by `File.materialize()` on first use, so loading a large dataset stays cheap."""
    out: list[EvalSample] = []
    for d in sorted(p for p in dataset_dir.iterdir() if p.is_dir()):
        sample_file = d / "sample.json"
        if not sample_file.exists():
            raise EvalTypeError(f"{d}: missing sample.json for sample {d.name!r}")
        sample = _parse_sample(json.loads(sample_file.read_text()), d, base_dir=d)
        if sample.data_point_id != d.name:
            raise EvalTypeError(
                f"{d}: sample.json's data_point_id {sample.data_point_id!r} does not match "
                f"the subdirectory name {d.name!r}")
        out.append(sample)
    return out


def load_dataset(spec: Any, *, repo_root: pathlib.Path | None = None) -> Dataset:
    """Load a Dataset from a TaskSpec (or a plain dict with an `evaluation_data` block).

    Samples come from an inline `evaluation_data.data` list if present, else a directory of
    per-sample subdirectories pointed at by `evaluation_data.dataset`. The config
    (type/max_turns/repeat) is read from `evaluation_data`.
    """
    ev = _evaluation_data(spec)
    if repo_root is None:
        from ..core.spec import REPO_ROOT
        repo_root = REPO_ROOT
    if ev.get("data") is not None:
        return parse_dataset(ev, list(ev["data"]))
    if ev.get("dataset"):
        dataset_dir = pathlib.Path(ev["dataset"])
        if not dataset_dir.is_absolute():
            dataset_dir = repo_root / dataset_dir
        if not dataset_dir.is_dir():
            raise EvalTypeError(f"eval dataset directory not found: {dataset_dir}")
        return _build_dataset(ev, _read_sample_dirs(dataset_dir))
    raise EvalTypeError("evaluation_data has neither inline 'data' nor a 'dataset' pointer")


def _evaluation_data(spec: Any) -> dict:
    ev = getattr(spec, "evaluation_data", None)
    if ev is None and isinstance(spec, dict):
        ev = spec.get("evaluation_data")
    if not isinstance(ev, dict):
        raise EvalTypeError("spec has no evaluation_data block")
    return ev

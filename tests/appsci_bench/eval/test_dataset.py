"""Unit tests for dataset loading + validation."""
from __future__ import annotations

import json

import pytest

from appsci_bench.eval.dataset import load_dataset, parse_dataset
from appsci_bench.eval.datatypes import EvalTypeError

_ST = {"data_point_id": "a", "single_turn_input": {"text": "hi"}}
_MT = {"data_point_id": "b", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}


def test_parse_single_turn_ok():
    ds = parse_dataset({"type": "single_turn"}, [_ST])
    assert ds.type == "single_turn" and ds.max_turns == 10 and ds.repeat == 1
    assert ds.data[0].single_turn_input.text == "hi"


def test_parse_multi_turn_ok_with_defaults():
    ds = parse_dataset({"type": "multi_turn", "max_turns": 5, "repeat": 3}, [_MT])
    assert ds.max_turns == 5 and ds.repeat == 3
    assert ds.data[0].multi_turn_input.user_stop_criteria  # default applied


@pytest.mark.parametrize("cfg", [{"type": "bogus"}, {}, {"type": "single_turn", "repeat": 0},
                                 {"type": "single_turn", "max_turns": 0}])
def test_parse_rejects_bad_config(cfg):
    with pytest.raises(EvalTypeError):
        parse_dataset(cfg, [_ST])


def test_parse_rejects_duplicate_ids():
    with pytest.raises(EvalTypeError):
        parse_dataset({"type": "single_turn"}, [_ST, dict(_ST)])


def test_parse_rejects_wrong_turn_shape():
    # single_turn sample missing its input
    with pytest.raises(EvalTypeError):
        parse_dataset({"type": "single_turn"}, [{"data_point_id": "x"}])
    # single_turn sample carrying a multi_turn_input
    bad = {"data_point_id": "x", "single_turn_input": {"text": "h"},
           "multi_turn_input": {"user_task": "t", "user_persona": "p"}}
    with pytest.raises(EvalTypeError):
        parse_dataset({"type": "single_turn"}, [bad])
    # multi_turn dataset given a single_turn sample
    with pytest.raises(EvalTypeError):
        parse_dataset({"type": "multi_turn"}, [_ST])


def test_load_dataset_inline_data():
    spec = {"evaluation_data": {"type": "single_turn", "data": [_ST]}}
    ds = load_dataset(spec)
    assert len(ds.data) == 1


def test_load_dataset_from_sample_dir(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "sample.json").write_text(json.dumps(_ST))
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "sample.json").write_text(
        json.dumps({"data_point_id": "c", "single_turn_input": {"text": "yo"}}))
    spec = {"evaluation_data": {"type": "single_turn", "dataset": str(tmp_path)}}
    ds = load_dataset(spec)
    assert [s.data_point_id for s in ds.data] == ["a", "c"]


def test_load_dataset_rejects_directory_name_mismatch(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "sample.json").write_text(
        json.dumps({"data_point_id": "different", "single_turn_input": {"text": "hi"}}))
    spec = {"evaluation_data": {"type": "single_turn", "dataset": str(tmp_path)}}
    with pytest.raises(EvalTypeError):
        load_dataset(spec)


def test_load_dataset_rejects_missing_directory(tmp_path):
    spec = {"evaluation_data": {"type": "single_turn", "dataset": str(tmp_path / "nope")}}
    with pytest.raises(EvalTypeError):
        load_dataset(spec)


def test_load_dataset_rejects_sample_dir_without_sample_json(tmp_path):
    (tmp_path / "s1").mkdir()
    spec = {"evaluation_data": {"type": "single_turn", "dataset": str(tmp_path)}}
    with pytest.raises(EvalTypeError):
        load_dataset(spec)


def test_load_dataset_resolves_file_path_lazily(tmp_path):
    sample_dir = tmp_path / "s1"
    sample_dir.mkdir()
    (sample_dir / "attachment.txt").write_bytes(b"hello world")
    (sample_dir / "sample.json").write_text(json.dumps({
        "data_point_id": "s1",
        "single_turn_input": {"text": "hi", "files": [
            {"file_type": "txt", "file_name": "attachment.txt", "file_path": "attachment.txt"}]},
    }))
    spec = {"evaluation_data": {"type": "single_turn", "dataset": str(tmp_path)}}
    ds = load_dataset(spec)
    f = ds.data[0].single_turn_input.files[0]
    assert f.file_data == b""            # not read at load time
    f.materialize()
    assert f.file_data == b"hello world"


def test_load_dataset_rejects_missing_file_path_target(tmp_path):
    sample_dir = tmp_path / "s1"
    sample_dir.mkdir()
    (sample_dir / "sample.json").write_text(json.dumps({
        "data_point_id": "s1",
        "single_turn_input": {"text": "hi", "files": [
            {"file_type": "txt", "file_name": "gone.txt", "file_path": "gone.txt"}]},
    }))
    spec = {"evaluation_data": {"type": "single_turn", "dataset": str(tmp_path)}}
    with pytest.raises(EvalTypeError):
        load_dataset(spec)

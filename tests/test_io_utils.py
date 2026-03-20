import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from src.io_utils import atomic_write_json, atomic_append_jsonl, atomic_write_jsonl, read_jsonl

def test_atomic_write_json(tmp_path):
    path = tmp_path / "test.json"
    data = {"key": "value", "num": 42}
    atomic_write_json(str(path), data)
    assert json.loads(path.read_text()) == data

def test_atomic_write_json_no_partial_on_error(tmp_path):
    path = tmp_path / "test.json"
    atomic_write_json(str(path), {"initial": True})
    with pytest.raises(TypeError):
        atomic_write_json(str(path), {"bad": object()})
    assert json.loads(path.read_text()) == {"initial": True}
    assert not (tmp_path / "test.json.tmp").exists()

def test_atomic_append_jsonl(tmp_path):
    path = tmp_path / "test.jsonl"
    atomic_append_jsonl(str(path), {"a": 1})
    atomic_append_jsonl(str(path), {"b": 2})
    lines = read_jsonl(str(path))
    assert lines == [{"a": 1}, {"b": 2}]

def test_read_jsonl_empty(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_text("")
    assert read_jsonl(str(path)) == []

def test_read_jsonl_missing(tmp_path):
    path = tmp_path / "missing.jsonl"
    assert read_jsonl(str(path)) == []

def test_read_jsonl_skips_malformed_lines(tmp_path, caplog):
    path = tmp_path / "test.jsonl"
    path.write_text('{"a":1}\n8,"revision":37}\n{"b":2}\n')
    with caplog.at_level(logging.WARNING):
        lines = read_jsonl(str(path))
    assert lines == [{"a": 1}, {"b": 2}]
    assert "Skipping malformed JSONL line" in caplog.text

def test_atomic_write_jsonl(tmp_path):
    path = tmp_path / "test.jsonl"
    atomic_write_jsonl(str(path), [{"a": 1}, {"b": 2}])
    assert read_jsonl(str(path)) == [{"a": 1}, {"b": 2}]

def test_atomic_write_json_concurrent(tmp_path):
    path = tmp_path / "shared.json"

    def _write(i: int):
        atomic_write_json(str(path), {"i": i})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, range(25)))

    payload = json.loads(path.read_text())
    assert "i" in payload

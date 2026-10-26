"""tools/pt_to_meta_weights.write_outputs の並行書き込みの試験。

2 つのエッジが同じマシンの同じ storage/dist の同じバッチを同時に変換すると、固定名の一時ファイル（weight.bin.tmp・meta.json.tmp）を
奪い合い、後から replace した側が FileNotFoundError で「自動変換失敗」になった（2026-10-21 のラボ）。同じファイルに同時に
書くと壊れる危険もある。一時ファイル名を書き込みごとに一意にして、最終ファイルが常に完全であることを固定する。
"""
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from tools import pt_to_meta_weights as conv


def _payload(n_bytes=4 * 1024 * 1024):
    data = (b"\x00\x00\x80\x3f" * (n_bytes // 4))
    entries = [{"name": "w", "shape": [n_bytes // 4], "dtype": "float32", "offset": 0, "nbytes": len(data)}]
    return data, entries


def test_concurrent_writers_do_not_fail_and_leave_a_complete_result(tmp_path):
    data, entries = _payload()
    errors = []
    start = threading.Barrier(8)

    def run():
        try:
            start.wait()
            conv.write_outputs(tmp_path, data, entries, "v-test", [])
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    w = tmp_path / "weight.bin"
    assert hashlib.sha256(w.read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest()
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["weights_size"] == len(data)


def test_no_temporary_files_are_left_behind(tmp_path):
    data, entries = _payload(1024)
    conv.write_outputs(tmp_path, data, entries, "v", [])
    conv.write_outputs(tmp_path, data, entries, "v", [])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["meta.json", "weight.bin"]


def test_failed_write_leaves_no_temporary_files(tmp_path, monkeypatch):
    """書き込みの途中で失敗しても、一意な名前の一時ファイルが孤児として残らない（レビュー指摘）。"""
    data, entries = _payload(1024)
    real_replace = Path.replace

    def boom(self, target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", boom)
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError):
        conv.write_outputs(tmp_path, data, entries, "v", [])
    monkeypatch.setattr(Path, "replace", real_replace)
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

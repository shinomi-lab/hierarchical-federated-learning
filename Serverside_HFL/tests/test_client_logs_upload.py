"""POST /upload_client_logs/{terminal_id} の試験。

2026-10-22: 端末の UploadLogsWorker が毎回 400「no non-empty log files (0-byte uploads rejected)」になっていた。
原因は _sha256_streaming_path が total（読んだバイト数）をファイルを読む前の 0 のまま返し、呼び出し側が全てのファイルを
空として捨てていたこと。正常な内容のファイルを送って保存されること、本当に空なら 400 のままであることを固定する。
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def api(tmp_path, monkeypatch):
    from edge_server.endpoints import client_logs

    monkeypatch.setattr(client_logs, "TERMINAL_LOGS_DIR", tmp_path)
    app = FastAPI()
    app.include_router(client_logs.router)
    return TestClient(app), tmp_path


def _meta(kind="bulk_logs"):
    return json.dumps({"terminal_id": "terminal-01", "round": 3, "timestamp_ms": 1, "report_kind": kind})


def _files(n=3, body=b"line\n" * 50):
    return [("file_%d" % i, ("log%d.txt" % i, body + (b"%d" % i), "text/plain")) for i in range(n)]


def test_non_empty_files_are_stored(api):
    client, root = api
    r = client.post("/upload_client_logs/terminal-01", files=_files(3), data={"meta": _meta()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["received"]) == 3
    assert all(item["size"] > 0 for item in body["received"])
    stored = [Path(item["stored_path"]) for item in body["received"]]
    assert all(p.exists() and p.stat().st_size > 0 for p in stored)
    assert stored[0].read_bytes().startswith(b"line\n")


def test_same_content_twice_is_a_duplicate_not_an_error(api):
    client, _ = api
    files = _files(1)
    assert client.post("/upload_client_logs/terminal-01", files=files, data={"meta": _meta()}).status_code == 200
    r = client.post("/upload_client_logs/terminal-01", files=files, data={"meta": _meta()})
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate_ignored"


def test_empty_files_only_are_still_rejected(api):
    client, _ = api
    r = client.post("/upload_client_logs/terminal-01", files=[("file_0", ("empty.txt", b"", "text/plain"))], data={"meta": _meta()})
    assert r.status_code == 400
    assert "0-byte" in r.json()["message"]


def test_a_mix_of_empty_and_non_empty_stores_the_non_empty(api):
    client, _ = api
    files = [("file_0", ("empty.txt", b"", "text/plain"))] + _files(1)
    r = client.post("/upload_client_logs/terminal-01", files=files, data={"meta": _meta()})
    assert r.status_code == 200
    assert len(r.json()["received"]) == 1


def test_error_report_with_only_empty_files_is_accepted(api):
    client, _ = api
    r = client.post("/upload_client_logs/terminal-01", files=[("file_0", ("empty.txt", b"", "text/plain"))],
                    data={"meta": _meta("error_report")})
    assert r.status_code == 200
    assert r.json()["status"] == "no_files_received"

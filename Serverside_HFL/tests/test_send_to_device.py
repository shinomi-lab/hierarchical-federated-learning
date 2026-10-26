from fastapi.testclient import TestClient
from edge_server import startup
from edge_server.main import create_app

# 2026-10 (SP3b 2.5) の修正点: 旧テストはディスクに存在しないバッチ（20251215_121116）を app.state.package に
# 入れるだけで links が組み立てられることを期待していた。現在の /send_to_device は、変換済みの weight.bin が
# 無いバッチを配らない（.pt へフォールバックせず 503/202 を返す）。端末が読めない .pt を配らないための意図した
# 挙動なので、テスト側で配布ディレクトリ（tmp_path）に weight.bin・meta.json・CSV を用意してから確認する。


def _make_batch(base, name):
    d = base / name
    d.mkdir(parents=True)
    (d / "global_model_mobile.pt").write_bytes(b"PT")
    (d / "weight.bin").write_bytes(b"\x00" * 8)
    (d / "meta.json").write_text("{}", encoding="utf-8")
    (d / "latest_data.csv").write_text("a,b\n", encoding="utf-8")


def test_send_to_device_links_and_meta(tmp_path, monkeypatch):
    base = (tmp_path / "received_files").resolve()
    _make_batch(base, "20251215_121116")
    monkeypatch.setattr(startup, "BASE_DIR", base)

    app = create_app()
    # set a fake package in app.state
    app.state.package = {
        "model_rel": "20251215_121116/global_model_mobile.pt",
        "data_rel": "20251215_121116/latest_data.csv",
        "model_meta": {"content_sha256": "sha256:deadbeef", "expected_size_bytes": 12345, "media_type": "application/octet-stream"},
        "data_meta": {"content_sha256": "sha256:abcd", "expected_size_bytes": 100}
    }

    client = TestClient(app)
    resp = client.get("/send_to_device")
    assert resp.status_code == 200
    body = resp.json()
    assert "links" in body
    links = body["links"]
    assert links["model_bin"].endswith("rel_path=20251215_121116/weight.bin")
    assert links["model_meta"].endswith("rel_path=20251215_121116/meta.json")
    assert "model_rel" in body.get("paths", {})
    # meta should include verification fields
    assert body.get("meta", {}).get("model", {}).get("content_sha256") == "sha256:deadbeef"


def test_send_to_device_refuses_batch_without_weight_bin(tmp_path, monkeypatch):
    """変換前（.pt のみ）のバッチは配らない（端末は .pt を読めない）。"""
    base = (tmp_path / "received_files").resolve()
    (base / "20251215_121116").mkdir(parents=True)
    (base / "20251215_121116" / "global_model_mobile.pt").write_bytes(b"PT")
    monkeypatch.setattr(startup, "BASE_DIR", base)

    app = create_app()
    app.state.package = {"model_rel": "20251215_121116/global_model_mobile.pt"}
    resp = TestClient(app).get("/send_to_device")
    assert resp.status_code == 503

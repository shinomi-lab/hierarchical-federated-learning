from fastapi.testclient import TestClient
from edge_server.main import create_app


def test_send_to_device_links_and_meta():
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
    assert "model_bin" in links
    assert "model_meta" in links
    assert "model_rel" in body.get("paths", {})
    # meta should include verification fields
    assert body.get("meta", {}).get("model", {}).get("content_sha256") == "sha256:deadbeef"

"""F9/F10: 中央から届いた新モデルが変換され、端末への配布情報に反映されること。"""
from central_server.endpoints import edge_update
from edge_server import startup
from edge_server.endpoints import model_ops


def test_converter_script_paths_exist():
    # F9: 受信時の自動変換が参照するスクリプトが実在する（旧: edge_server/tools, central_server/tools を指していた）
    assert model_ops.CONVERTER_SCRIPT.is_file()
    assert edge_update.CONVERTER_SCRIPT.is_file()


def _batch(base, name, converted=True, data=False):
    d = base / name
    d.mkdir()
    (d / "global_model_mobile.pt").write_bytes(b"pt")
    if converted:
        (d / "weight.bin").write_bytes(b"\0" * 4)
        (d / "meta.json").write_text("{}")
    if data:
        (d / "latest_data.csv").write_text("x")


def test_latest_package_prefers_newest_converted_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "BASE_DIR", tmp_path)
    _batch(tmp_path, "20261013_100000", data=True)
    _batch(tmp_path, "20261013_100100")
    _batch(tmp_path, "20261013_100200", converted=False)  # 変換中（.pt のみ）は配らない
    assert startup._latest_package_from_disk()["model_rel"] == "20261013_100100/global_model_mobile.pt"


def test_choose_package_newer_disk_wins_and_keeps_training_data():
    # F10: 起動時パッケージより新しい変換済みバッチがあればそちらを配る（学習データは起動時のものを保持）
    state = {"model_rel": "20261013_100000/global_model_mobile.pt", "data_rel": "20261013_100000/latest_data.csv"}
    disk = {"model_rel": "20261013_100100/global_model_mobile.pt"}
    assert startup._choose_package(state, disk) == {
        "model_rel": "20261013_100100/global_model_mobile.pt", "data_rel": "20261013_100000/latest_data.csv"}


def test_choose_package_older_or_missing_disk_keeps_state():
    state = {"model_rel": "20261013_100100/global_model_mobile.pt"}
    assert startup._choose_package(state, {"model_rel": "20261013_100000/global_model_mobile.pt"}) == state
    assert startup._choose_package(state, None) == state
    assert startup._choose_package(None, state) == state

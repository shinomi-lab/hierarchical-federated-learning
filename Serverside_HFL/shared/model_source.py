# Serverside_HFL/shared/model_source.py
"""推論に使うグローバルモデルの出所とラウンドの判定（start.py の推論スナップショットと、hfl_rec_infer が共有する）。"""
import datetime as _dt
import json
from pathlib import Path


def snapshot_sources(storage_model: Path, initial_model: Path, meta_path: Path) -> dict:
    """推論スナップショットが使うモデルとラウンド。保存先にモデルが無ければ初期モデルを使う。

    is_initial: 初期モデル（集約前）か。保存先にモデルが無いとき、またはラウンドが 1（集約がまだ起きていない）のとき True。
    リポジトリから直接起動すると保存先とコードの state/ が同じファイルになるので、ファイルの場所だけでは判定できない。
    """
    use_initial = not Path(storage_model).exists()
    model = Path(initial_model) if use_initial else Path(storage_model)
    try:
        mtime = _dt.datetime.fromtimestamp(model.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        mtime = None
    try:
        rnd = json.loads(Path(meta_path).read_text(encoding="utf-8")).get("round")
    except (OSError, ValueError):
        rnd = None
    is_initial = use_initial or (isinstance(rnd, int) and rnd <= 1)
    return {"model_path": model, "is_initial": is_initial, "mtime": mtime, "round": rnd}

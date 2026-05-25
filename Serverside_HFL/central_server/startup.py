# central_server/startup.py

import os
import sys
import shutil
import json
import torch
import asyncio
import hashlib
from fastapi import FastAPI
from pathlib import Path

# 親ディレクトリを sys.path に追加して、ルートのモジュールをインポート可能にする
sys.path.append(str(Path(__file__).resolve().parent.parent))

from central_server.config import (
    GLOBAL_MODEL_PATH, CONFIG_PATH,
    TRAINING_DATA_DIR, SOURCE_DATA_PATH, LATEST_DATA_CSV_PATH,
    META_PATH,
)
from central_server import state
from create_torchscript_model import create_initial_model  # ← 既存のモデル定義を利用
from central_server.endpoints.utils import utils_router
from central_server.utils.trace_logging import log_calls

def _sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


@log_calls()
def prepare_training_data():
    """
    教師データの準備:
      - SOURCE_DATA_PATH があれば LATEST_DATA_CSV_PATH にコピー（初回だけ）
      - 既に latest があるなら上書きしない
      - 無ければ警告だけ出してスキップ（起動は続行）
    """
    TRAINING_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LATEST_DATA_CSV_PATH.exists():
        print(f"教師データは既に存在: '{LATEST_DATA_CSV_PATH}'（上書きしません）")
        return

    if os.path.exists(SOURCE_DATA_PATH):
        shutil.copy2(SOURCE_DATA_PATH, LATEST_DATA_CSV_PATH)
        print(f"教師データをコピー: '{SOURCE_DATA_PATH}' → '{LATEST_DATA_CSV_PATH}'")
    else:
        print(f"⚠ 教師データのソースが見つかりません: '{SOURCE_DATA_PATH}'（スキップ）")


@log_calls()
def load_or_initialize_state_dict() -> Path:
    """
    中央の初期グローバルを state_dict で用意する。
    既存の GLOBAL_MODEL_PATH があればそれを利用。壊れていれば初期化にフォールバック。
    戻り値は最終的に使用するファイルパス。
    """
    final_path = Path(GLOBAL_MODEL_PATH).resolve()
    final_path.parent.mkdir(parents=True, exist_ok=True)

    if final_path.exists():
        print(f"既存のグローバル state_dict をロード: {final_path}")
        # まず state_dict として読み込めるか試す
        try:
            sd = torch.load(str(final_path), map_location="cpu")
            if isinstance(sd, dict) and sd:
                return final_path
            # それ以外（例えば ScriptModule が返る場合）は以下でハンドル
            raise RuntimeError("not a state_dict dict")
        except Exception as e:
            # state_dict として読み込めなかった場合、TorchScript モジュールとして読み込みを試みる
            try:
                print(f"[INFO] state_dict での読み込み失敗 ({e}); TorchScript として読み込みを試みます")
                mod = torch.jit.load(str(final_path), map_location="cpu")
                try:
                    sd = mod.state_dict()
                    # 抽出した state_dict を別ファイルへ保存して上書きを避ける
                    extracted = final_path.with_name(final_path.stem + "_extracted_state.pt")
                    torch.save(sd, str(extracted))
                    print(f"[INFO] TorchScript から state_dict を抽出して保存しました: {extracted}")
                    return extracted
                except Exception as ex2:
                    print(f"[WARN] TorchScript は読み込めたが state_dict 抽出に失敗: {ex2}")
            except Exception:
                print(f"[WARN] 既存モデルのロード失敗、初期化に切り替えます: {e}")

    # 初期作成：モデルを作って state_dict を保存
    print(f"新規グローバル state_dict を作成: {final_path}")
    model = create_initial_model()  # nn.Module を返す想定
    sd = model.state_dict()
    try:
        from central_server.utils.safe_io import safe_save_state_dict

        safe_save_state_dict(final_path, sd)
    except Exception:
        # best-effort fallback
        torch.save(sd, str(final_path))
    # デバッグ: 保存した絶対パスと存在確認ログを出力
    try:
        abs_p = str(final_path.resolve())
        exists = final_path.exists()
        print(f"Saved initial global state_dict to: {abs_p} (exists={exists})")
    except Exception:
        pass
    return final_path


@log_calls()
def ensure_initial_meta(model_pt: Path):
    """
    META_PATH が無ければ初期メタを書き出し。
    既存の META があっても、base_hash は実ファイルの sha256 で毎回同期する。
    """
    base_hash = _sha256_hex(model_pt) if model_pt.exists() else "sha256:bootstrap"

    if META_PATH.exists():
        try:
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
            # base_hash を常に最新へ更新（他は保持）
            old = meta.get("base_hash")
            meta["base_hash"] = base_hash
            META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            if old != base_hash:
                print(f"メタの base_hash を更新: {old} → {base_hash}")
            else:
                print("メタの base_hash は最新です。")
            return
        except Exception as e:
            print(f"[WARN] 既存 META の読み込みに失敗。再生成します: {e}")

    meta = {
        "round": 1,
        "model_id": "demo-mlp-v1",
        "base_hash": base_hash,  # 実ファイルのハッシュで整合
        "preferred_dtype": "torch_state_dict",  # f32_flat を使うなら "f32_flat" に
        "upload_endpoint": "/receive_terminal_weights/{terminal_id}",
        "aggregation_threshold": 1,
        "expected_param_len": 0,
        "order_version": 1,
    }
    META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"初期メタを書き出し: {META_PATH}")


def register_startup_events(app: FastAPI):
    @app.on_event("startup")
    async def startup_event():
        # 1) 教師データ
        prepare_training_data()

        # 2) モデル（既存を優先、壊れていれば初期化）
        model_pt = load_or_initialize_state_dict()

        # 3) メタ（base_hash を model_pt に同期）
        ensure_initial_meta(model_pt)

        # 4) 中央の状態へロードした state_dict を載せる
        try:
            sd = await asyncio.to_thread(torch.load, str(model_pt), map_location="cpu")
            if not isinstance(sd, dict) or not sd:
                raise RuntimeError("invalid state_dict after load")
            state.global_model_state["state_dict"] = sd  # ← central_server/state.py 側で参照
            print(f"[INFO] state_dict successfully loaded: {list(sd.keys())}")
        except Exception as e:
            # 万一ここで失敗したら明確にログを出す
            print(f"[FATAL] モデル state_dict のロードに失敗しました: {e}")
            raise

        # 5) 環境変数の起動時確認ログ（MARKER_DB_PATH / HFL_STORAGE_DIR）
        try:
            marker_env = os.environ.get('MARKER_DB_PATH')
            storage_env = os.environ.get('HFL_STORAGE_DIR')
            print(f"STARTUP: MARKER_DB_PATH={marker_env or 'unset'}")
            print(f"STARTUP: HFL_STORAGE_DIR={storage_env or 'unset'}")
        except Exception:
            # best-effort, do not fail startup
            pass

    # Register the utils router (converted from Flask Blueprint to FastAPI APIRouter)
    app.include_router(utils_router, prefix='/utils')

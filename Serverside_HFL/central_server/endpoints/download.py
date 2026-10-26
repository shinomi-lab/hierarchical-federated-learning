# central_server/endpoints/download.py
import os
import json
import mimetypes
import traceback
import glob
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from central_server.config import (
    GLOBAL_MODEL_PATH, CONFIG_PATH, LATEST_DATA_CSV_PATH,
    STATE_DIR, DEFAULT_MODEL_NAME, RECEIVED_EDGES_DIR
)
# Prefer the META_PATH used by the edge_update module (authoritative write target)
from central_server.endpoints.edge_update import META_PATH as EU_META_PATH

router = APIRouter()


# ========= グローバルモデル情報配布（堅牢版） =========
@router.get("/get_global_model")
def get_global_model():
    """
    エッジ起動時に叩かれるエンドポイント。
    app.json または current_meta.json を返し、
    モデルが存在しない場合やエラー時にも 500 JSON で原因を明示。
    """
    try:
        # --- モデル存在チェック ---
        if not os.path.exists(GLOBAL_MODEL_PATH):
            # fallback: state 内の *.pt 検索
            candidates = sorted(glob.glob(str((STATE_DIR / "*.pt").resolve())))
            if not candidates:
                raise FileNotFoundError(f"No model found under {STATE_DIR}")
        else:
            candidates = [GLOBAL_MODEL_PATH]

        # --- コンフィグ読み込み（edge_update.META_PATH を優先し、なければ repo-level CONFIG_PATH を使う） ---
        cfg_path = EU_META_PATH if os.path.exists(EU_META_PATH) else CONFIG_PATH
        if not os.path.exists(cfg_path):
            raise FileNotFoundError(f"Neither app.json nor current_meta.json found")

        with open(cfg_path, "r", encoding="utf-8") as f:
            config_json = json.load(f)

        # --- 教師データ確認 ---
        data_path = "/download_training_data" if os.path.exists(LATEST_DATA_CSV_PATH) else None

        # --- レスポンス ---
        return {
            "model_path": "/download_global_model",
            "model_filename": os.path.basename(candidates[0]),
            "config": config_json,
            "data_path": data_path,
            "config_source": "app.json" if cfg_path == CONFIG_PATH else "current_meta.json",
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "detail": "get_global_model_failed",
                "error": str(e),
                "paths": {
                    "GLOBAL_MODEL_PATH": str(GLOBAL_MODEL_PATH),
                    "CONFIG_PATH": str(CONFIG_PATH),
                    "LATEST_DATA_CSV_PATH": str(LATEST_DATA_CSV_PATH),
                    "META_PATH": str(META_PATH),
                    "STATE_DIR": str(STATE_DIR),
                },
                "exists": {
                    "model": os.path.exists(GLOBAL_MODEL_PATH),
                    "config": os.path.exists(CONFIG_PATH),
                    "meta": os.path.exists(META_PATH),
                    "data": os.path.exists(LATEST_DATA_CSV_PATH),
                },
                "traceback": "".join(traceback.format_exception(type(e), e, e.__traceback__))[:3000],
            },
        )


# ========= モデル実体ダウンロード（堅牢版） =========
@router.get("/download_global_model")
def download_global_model():
    """
    エッジがダウンロードする実体モデル。
    1) GLOBAL_MODEL_PATH があればそれを返す
    2) 無ければ STATE_DIR の *.pt から最初の1つを返す
    """
    if os.path.exists(GLOBAL_MODEL_PATH):
        fp = GLOBAL_MODEL_PATH
    else:
        candidates = sorted(glob.glob(str((STATE_DIR / "*.pt").resolve())))
        if not candidates:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": "No model file found",
                    "expected": str(GLOBAL_MODEL_PATH),
                    "searched_dir": str(STATE_DIR),
                    "pt_candidates": candidates,
                },
            )
        fp = Path(candidates[0])

    return FileResponse(
        fp,
        media_type="application/octet-stream",
        filename=os.path.basename(fp),
    )


# ========= コンフィグ・データダウンロード =========
@router.get("/download_config")
def download_config():
    """app.json または current_meta.json の内容を返す"""
    cfg_path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else META_PATH
    if not os.path.exists(cfg_path):
        raise HTTPException(404, "app.json も current_meta.json も見つかりません")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return JSONResponse(content=json.load(f))


@router.get("/download_training_data")
def download_training_data():
    if not os.path.exists(LATEST_DATA_CSV_PATH):
        raise HTTPException(404, "教師データが見つかりません")
    return FileResponse(
        LATEST_DATA_CSV_PATH,
        media_type="text/csv",
        filename="training_data.csv",
    )


# ========= 診断用 =========
@router.get("/_diag/paths")
def diag_paths():
    return {
        "GLOBAL_MODEL_PATH": str(GLOBAL_MODEL_PATH),
        "CONFIG_PATH": str(CONFIG_PATH),
        "LATEST_DATA_CSV_PATH": str(LATEST_DATA_CSV_PATH),
        "META_PATH": str(META_PATH),
        "STATE_DIR": str(STATE_DIR),
        "exists": {
            "model": os.path.exists(GLOBAL_MODEL_PATH),
            "config": os.path.exists(CONFIG_PATH),
            "meta": os.path.exists(META_PATH),
            "data": os.path.exists(LATEST_DATA_CSV_PATH),
        },
    }

# ========= 受信済みエッジ集約モデルのダウンロード用 =========

@router.get("/received_edges", summary="List all received edge models")
def list_received_edges():
    """
    `received_edges` ディレクトリ配下にある、エッジからアップロードされた
    集約済みモデルのファイル一覧を返します。
    """
    if not RECEIVED_EDGES_DIR.exists():
        return {"received_edges": []}

    files = []
    # パスを再帰的に探索
    for p in RECEIVED_EDGES_DIR.rglob("*.pt"):
        try:
            # `received_edges` からの相対パスを取得
            rel_path = p.relative_to(RECEIVED_EDGES_DIR)
            parts = rel_path.parts
            if len(parts) >= 3:
                files.append({
                    "round_id": parts[0],
                    "edge_id": parts[1],
                    "filename": parts[2],
                    "path": str(rel_path).replace("\\", "/"), # Windowsパス対策
                    "size_bytes": p.stat().st_size,
                    "modified_time": p.stat().st_mtime,
                })
        except (ValueError, IndexError):
            continue # パス構造が想定と異なる場合はスキップ
    return {"received_edges": sorted(files, key=lambda x: x["path"], reverse=True)}

@router.get("/download_received_edge/{round_id}/{edge_id}/{filename}", summary="Download a specific received edge model")
def download_received_edge(round_id: str, edge_id: str, filename: str):
    """指定されたラウンド、エッジID、ファイル名の集約済みモデルをダウンロードします。"""
    file_path = RECEIVED_EDGES_DIR / round_id / edge_id / filename
    return FileResponse(file_path, media_type="application/octet-stream", filename=filename)

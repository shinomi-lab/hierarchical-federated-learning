# edge_server/utils/files_api.py
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
import mimetypes
import json
from typing import Optional

from edge_server.config import (
    RECEIVED_DIR,
    DEFAULT_MODEL_NAME,
    DEFAULT_CONFIG_NAME,
)

# 保存時の訓練データ名は startup と合わせる
TRAINING_DATA_FILENAME = "latest_data.csv"

BASE_DIR = Path(RECEIVED_DIR).resolve()


def _safe_join(rel_path: str) -> Path:
    """
    BASE_DIR 配下の相対パスのみ許可（絶対パス/ドライブ指定/外部脱出は拒否）。
    """
    if not rel_path or rel_path.startswith("/") or ":" in rel_path:
        raise HTTPException(status_code=404, detail="見つかりません")
    p = (BASE_DIR / rel_path).resolve()
    if not str(p).startswith(str(BASE_DIR)) or not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="見つかりません")
    return p


def _safe_from_abs(abs_path: str) -> Path:
    """
    旧クライアント互換: 絶対パス(?path=C:\...) を受けた際に、
    BASE_DIR 配下のファイルだけを許可する。
    """
    p = Path(abs_path).resolve()
    if not str(p).startswith(str(BASE_DIR)) or not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="見つかりません")
    return p


def _pick_first_existing(candidates: list[Path]) -> Optional[Path]:
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return p
        except Exception:
            # 権限/不正名などはスキップ
            continue
    return None


def _latest_package_from_disk() -> Optional[dict]:
    """
    BASE_DIR 直下の最新タイムスタンプディレクトリから相対パス群を作る。
    まずは既定名を探し、無ければ拡張子でフォールバック（柔軟化）。
    戻り値例:
    {"model_rel": "20251008_112233/model.pt", "config_rel": ".../app.json", "data_rel": ".../latest_data.csv"}
    """
    if not BASE_DIR.exists():
        return None
    dirs = [p for p in BASE_DIR.iterdir() if p.is_dir()]
    if not dirs:
        return None
    latest = max(dirs, key=lambda p: p.name)

    # モデル
    model_candidates = [latest / DEFAULT_MODEL_NAME, *latest.glob("*.pt"), *latest.glob("*.pth")]
    model_fp = _pick_first_existing(model_candidates)

    # 教師データ
    data_candidates = [latest / TRAINING_DATA_FILENAME, *latest.glob("*.csv")]
    data_fp = _pick_first_existing(data_candidates)

    pkg: dict[str, str] = {}
    if model_fp:  pkg["model_rel"]  = f"{latest.name}/{model_fp.name}"
    if data_fp:   pkg["data_rel"]   = f"{latest.name}/{data_fp.name}"
    return pkg or None


def register_api_routes(app: FastAPI) -> None:
    """
    [未使用] /download と /send_to_device の旧実装。
    main.py は startup.py の register_api_routes を使用しているため、
    この関数は呼び出されない。startup.py に統一済み。
    """
    import logging
    log = logging.getLogger("edge.files_api")
    try:
        log.info("ダウンロード基準ディレクトリ: %s 存在=%s", str(BASE_DIR), BASE_DIR.exists())
    except Exception:
        pass

    @app.get("/download")
    def download(
        rel_path: Optional[str] = Query(default=None),
        path: Optional[str] = Query(default=None),  # 旧クライアント互換: 絶対パスを受ける
    ):
        """
        ダウンロード（新: rel_path / 旧: path の両受け）。
        - rel_path が優先
        - path は BASE_DIR 配下のみ許可
        """
        if rel_path is None and path is None:
            raise HTTPException(status_code=422, detail="rel_path または path のどちらかが必要です")

        if rel_path:
            try:
                p = _safe_join(rel_path)
            except HTTPException:
                # Log missing file access attempt for debugging and re-raise
                try:
                    log.warning("ダウンロード要求がありましたがファイルが見つかりません: %s", rel_path)
                except Exception:
                    pass
                raise
        else:
            p = _safe_from_abs(path)  # type: ignore[arg-type]

        mime, _ = mimetypes.guess_type(p.name)
        return FileResponse(p, media_type=mime or "application/octet-stream", filename=p.name)

    @app.get("/send_to_device")
    def send_to_device(request: Request):
        """
        端末へ配布するモデル/設定/教師データの場所を返す。
        - 新フォーマット: paths + links
        - 後方互換: legacy（model/config/dataオブジェクト）も同梱
        """
        # 起動時に startup 側で詰めたものを優先、なければディスク走査
        pkg = getattr(app.state, "package", None) or _latest_package_from_disk()
        if not pkg:
            return JSONResponse({"detail": "No package ready"}, status_code=503)

        # ベースURL
        scheme = request.url.scheme
        host = request.headers.get("host") or f"{request.client.host}"
        base_url = f"{scheme}://{host}"

        def link(rel: str) -> str:
            # 端末はこのURLをそのままGETすればよい
            return f"{base_url}/download?rel_path={rel}"

        paths = {k: v for k, v in pkg.items()}
        links = {
            "model":  link(pkg["model_rel"])  if "model_rel"  in pkg else None,
            "config": link(pkg["config_rel"]) if "config_rel" in pkg else None,
            "data":   link(pkg["data_rel"])   if "data_rel"   in pkg else None,
        }

        # 後方互換: 旧クライアントが見る "model/config/data" も入れる
        def abs_path(rel_key: str) -> Optional[str]:
            if rel_key not in pkg:
                return None
            return str((BASE_DIR / pkg[rel_key]).resolve())

        legacy = {
            "model": (
                {
                    "path": abs_path("model_rel"),
                    "status_code": 200,
                    "media_type": "application/octet-stream",
                } if "model_rel" in pkg else None
            ),
            "data": (
                {
                    "path": abs_path("data_rel"),
                    "status_code": 200,
                    "media_type": "text/csv",
                } if "data_rel" in pkg else None
            ),
        }

        return {
            "paths": paths,
            "links": links,
            # 旧クライアント互換
            **legacy,
        }

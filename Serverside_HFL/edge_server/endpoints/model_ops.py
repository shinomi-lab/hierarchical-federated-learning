from __future__ import annotations

from fastapi import APIRouter, UploadFile, File, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pathlib import Path
import time
import json
import mimetypes
from typing import List, Optional, Union
from pydantic import BaseModel, Field

from edge_server.config import RECEIVED_DIR, DEFAULT_MODEL_NAME, DEFAULT_CONFIG_NAME
from edge_server.startup import find_bin_meta_for_model_rel
from edge_server.utils.file_utils import get_latest_batch_dir
from edge_server.utils.time_logger import edge_time_logger
from edge_server.state import current_edge_state, save_state, clear_round
from edge_server.endpoints.notifications import notifications_manager
from edge_server.endpoints.notifications import recent_notifications
import time
from pydantic import BaseModel
import subprocess
import sys
import shlex


class DeviceAck(BaseModel):
    device_id: str
    notification_id: str
    model_id: Optional[str] = None
    round: Optional[int] = None
    event_timestamp: Optional[str] = None


class DeviceEvent(BaseModel):
    event_type: str
    device_id: str
    notification_id: Optional[str] = None
    model_id: Optional[str] = None
    round: Optional[int] = None
    details: Optional[dict] = None
    event_timestamp: Optional[str] = None



router = APIRouter()

BASE_DIR = Path(RECEIVED_DIR).resolve()
TRAINING_DATA_FILENAME = "latest_data.csv"  # エッジ保存時のデータ名に合わせる


def _safe_join(*parts: Union[str, Path]) -> Path:
    """BASE_DIR配下に限定して安全に結合。外へ出る場合は 404。"""
    p = (BASE_DIR.joinpath(*parts)).resolve()
    if not str(p).startswith(str(BASE_DIR)):
        raise HTTPException(status_code=404, detail="見つかりません")
    return p

def _load_recent_starts() -> None:
    global recent_device_event_starts
    try:
        if RECENT_STARTS_PATH.exists():
            with open(RECENT_STARTS_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            parsed = {}
            for key_s, ts in raw.items():
                parts = key_s.split("||")
                # pad to 4
                while len(parts) < 4:
                    parts.append("")
                parsed[tuple(parts[:4])] = float(ts)
            recent_device_event_starts = parsed
        else:
            recent_device_event_starts = {}
    except Exception:
        # If anything fails, just start with empty map
        recent_device_event_starts = {}


def _save_recent_starts() -> None:
    try:
        # serialize tuple keys as '||' joined strings
        serializable = {"||".join(k): v for k, v in recent_device_event_starts.items()}
        tmp = RECENT_STARTS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serializable, f)
        tmp.replace(RECENT_STARTS_PATH)
    except Exception:
        # best-effort only
        pass


# load persisted starts at import
_load_recent_starts()


# =========================
#  /upload_biases（端末→エッジ）
# =========================

class BiasData(BaseModel):
    # Pydantic v2 推奨: Field(min_length=1)
    biases: list[float] = Field(min_length=1)
    terminal_id: Optional[str] = None  # 端末IDを付けたい場合はクライアントから渡す


@router.post("/upload_biases")
async def upload_biases(payload: BiasData):
    """
    端末から送られてくる学習済みバイアスを受け取り、タイムスタンプ付きで保存。
    例:
      {"biases":[0.1, -0.2, ...], "terminal_id":"device-001"}
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    save_dir = Path(RECEIVED_DIR) / ts
    save_dir.mkdir(parents=True, exist_ok=True)

    filename = f"biases_{payload.terminal_id or 'unknown'}.json"
    save_path = save_dir / filename

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(
            {"biases": payload.biases, "terminal_id": payload.terminal_id, "ts": ts},
            f,
            ensure_ascii=False,
            indent=2,
        )

    # 端末からの学習関連データ受信を明示的にログに残す
    try:
        edge_time_logger.log_event("biases_received", {"path": str(save_path), "terminal_id": payload.terminal_id, "ts": ts})
    except Exception:
        pass
    try:
        # すぐ見えるログにも出す
        import logging
        logging.getLogger("edge.model_ops").info("バイアス受信 %s -> %s", payload.terminal_id, str(save_path))
    except Exception:
        pass

    return {"message": "biases received", "path": str(save_path)}


# =========================
#  モデル＋設定 受領（中央→エッジ）
# =========================

@router.post("/receive_model_and_app")
async def receive_model_and_app(
    model: UploadFile = File(...)
):
    """
    中央→エッジへのアップロードを受け取り、RECEIVED_DIR/<ts>/ に保存。
    (app.json は扱わない)
    """
    if not model.filename.endswith((".pt", ".pth")):
        raise HTTPException(status_code=400, detail="モデルファイルの拡張子が不正です")

    ts = time.strftime("%Y%m%d_%H%M%S")
    save_dir = _safe_join(ts)
    save_dir.mkdir(parents=True, exist_ok=True)

    # モデルは標準名で保存（非同期で書き込み）
    model_path = save_dir / DEFAULT_MODEL_NAME
    import aiofiles
    try:
        # Write to a temp file, fsync, then atomically replace to ensure file is visible.
        # Use time_ns() to make the tmp path unique even under concurrent pushes (P0 fix).
        import time as _time
        tmp_path = model_path.with_name(f"{model_path.stem}_{_time.time_ns()}.tmp")
        async with aiofiles.open(tmp_path, "wb") as f:
            while True:
                chunk = await model.read(1024 * 1024)
                if not chunk:
                    break
                await f.write(chunk)
        # Ensure data is flushed to disk
        try:
            import os
            fd = os.open(str(tmp_path), os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            # best-effort fsync; continue even if it fails
            pass
        # atomic replace
        try:
            tmp_path.replace(model_path)
        except Exception:
            # fallback to os.replace
            import os as _os
            _os.replace(str(tmp_path), str(model_path))
    except Exception as e:
        # remove partial file if present and surface an error
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        try:
            if model_path.exists():
                model_path.unlink()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"アップロードしたモデルの保存に失敗しました: {e}")

    # Confirm file exists and is readable before notifying clients
    try:
        # small verification loop to guard against very short filesystem races
        import time as _time
        verified = False
        for _ in range(5):
            if model_path.exists() and model_path.stat().st_size > 0:
                verified = True
                break
            _time.sleep(0.05)
        if not verified:
            # log but continue
            try:
                import logging as _logging
                _logging.getLogger("edge.model_ops").warning("保存直後にモデルファイルが見えません: %s", str(model_path))
            except Exception:
                pass
        # Attempt to auto-convert .pt -> weight.bin + meta.json using the tools script
        try:
            # tools script lives at repository root
            script = Path(__file__).resolve().parents[1] / "tools" / "pt_to_meta_weights.py"
            cmd = [sys.executable, str(script), str(model_path), str(save_dir), "--model-version", ts]
            try:
                try:
                    edge_time_logger.log_event("auto_convert_started", {"batch": ts, "model_path": str(model_path)})
                except Exception:
                    pass
                try:
                    print(f"[Edge] 自動変換開始 batch={ts} model={model_path}")
                except Exception:
                    pass
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if proc.returncode != 0:
                    try:
                        edge_time_logger.log_error("auto_convert_failed", {"batch": ts, "rc": proc.returncode, "stderr": proc.stderr[:200]})
                    except Exception:
                        pass
                    try:
                        print(f"[Edge] 自動変換失敗 batch={ts} rc={proc.returncode} stderr={(proc.stderr or '')[:200]}")
                    except Exception:
                        pass
                else:
                    try:
                        edge_time_logger.log_event("auto_converted_bin_meta", {"batch": ts, "stdout": proc.stdout[:200]})
                    except Exception:
                        pass
                    try:
                        bin_rel, meta_rel = find_bin_meta_for_model_rel(f"{ts}/{DEFAULT_MODEL_NAME}")
                        print(f"[Edge] 自動変換完了(bin/meta) batch={ts} bin_rel={bin_rel} meta_rel={meta_rel}")
                    except Exception:
                        try:
                            print(f"[Edge] 自動変換完了 batch={ts}（bin/meta のパス解決に失敗）")
                        except Exception:
                            pass
            except Exception as e:
                try:
                    edge_time_logger.log_error("auto_convert_exception", {"batch": ts, "error": repr(e)})
                except Exception:
                    pass
                try:
                    print(f"[Edge] 自動変換で例外 batch={ts} error={repr(e)}")
                except Exception:
                    pass
        except Exception:
            # best-effort only; don't fail upload on conversion errors
            pass

            try:
                bin_rel, meta_rel = find_bin_meta_for_model_rel(f"{ts}/{DEFAULT_MODEL_NAME}")
                # Log both bin_rel and explicit model_rel for clarity
                edge_time_logger.log_event("pushed_model_received", {"batch": ts, "download_rel": bin_rel, "model_rel": f"{ts}/{DEFAULT_MODEL_NAME}"})
            except Exception:
                edge_time_logger.log_event("pushed_model_received", {"batch": ts, "model_rel": f"{ts}/{DEFAULT_MODEL_NAME}"})
        except Exception:
            edge_time_logger.log_event("pushed_model_received", {"batch": ts, "model_rel": f"{ts}/{DEFAULT_MODEL_NAME}"})
    except Exception:
        pass

    # ---- 新ラウンド反映（Edgeのメタ状態） ----
    try:
        prev_round = int(current_edge_state.round)
        # バックグラウンドポーラー（PULL）とのダブルバンプを防ぐ:
        # waiting_for_new_model=True の場合のみラウンドを進める。
        # False の場合はポーラーが既にラウンドを進めているので +1 しない。
        if current_edge_state.waiting_for_new_model:
            current_edge_state.round = prev_round + 1
        current_edge_state.waiting_for_new_model = False
        current_edge_state.is_aggregating = False
        # 次ラウンドの受信バケットをクリア（安全策）
        try:
            clear_round(current_edge_state.round)
        except Exception:
            pass
        # ログをラウンド専用ディレクトリへ切り替え
        try:
            from edge_server.utils.time_logger import edge_time_logger
            edge_time_logger.set_round(current_edge_state.round)
        except Exception:
            pass
        save_state(in_progress=False)
        # クライアントへWS通知（新モデル取得と新ラウンド案内）
        try:
            from edge_server.endpoints.notifications import broadcast_model_update
            try:
                bin_rel, meta_rel = find_bin_meta_for_model_rel(f"{ts}/{DEFAULT_MODEL_NAME}")
            except Exception:
                bin_rel = None
            # Only notify devices when weight.bin exists. Never send .pt via WS.
            if not bin_rel:
                try:
                    edge_time_logger.log_info("skip_notify_no_bin", {"batch": ts})
                except Exception:
                    pass
            else:
                try:
                    from edge_server.endpoints.notifications import broadcast_model_update
                    metadata = {
                        "round": int(current_edge_state.round),
                        "model_bin_rel": bin_rel,
                        "model_meta_rel": meta_rel,
                        "description": "auto_increment_on_receive_model",
                        "timestamp": ts,
                    }
                    await broadcast_model_update(metadata)
                except Exception:
                    # WS接続が無い場合はスキップ
                    pass
        except Exception:
            # WS接続が無い場合はスキップ
            pass
    except Exception:
        # 失敗してもアップロード成功は返す（ログのみ）
        try:
            edge_time_logger.log_event("round_increment_failed", {"batch": ts})
        except Exception:
            pass

    return {
        "status": "ok",
        "batch": ts,
        "paths": {
            "model_rel": f"{ts}/{DEFAULT_MODEL_NAME}",
        }
    }


@router.post("/receive_model")
async def receive_model(
    model: UploadFile = File(...)
):
    """
    中央からのモデル単体受信。`app.json` は送られない運用向けの軽量エンドポイント。
    受け取ったモデルファイルは `RECEIVED_DIR/<ts>/<DEFAULT_MODEL_NAME>` に保存する。
    """
    if not model.filename.endswith((".pt", ".pth")):
        raise HTTPException(status_code=400, detail="モデルファイルの拡張子が不正です")

    ts = time.strftime("%Y%m%d_%H%M%S")
    save_dir = _safe_join(ts)
    save_dir.mkdir(parents=True, exist_ok=True)

    model_path = save_dir / DEFAULT_MODEL_NAME
    import aiofiles
    try:
        # Use time_ns() to make tmp path unique under concurrent pushes (P0 fix).
        import time as _time
        tmp_path = model_path.with_name(f"{model_path.stem}_{_time.time_ns()}.tmp")
        async with aiofiles.open(tmp_path, "wb") as f:
            while True:
                chunk = await model.read(1024 * 1024)
                if not chunk:
                    break
                await f.write(chunk)
        try:
            import os
            fd = os.open(str(tmp_path), os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            pass
        try:
            tmp_path.replace(model_path)
        except Exception:
            import os as _os
            _os.replace(str(tmp_path), str(model_path))
    except Exception as e:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        try:
            if model_path.exists():
                model_path.unlink()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"アップロードしたモデルの保存に失敗しました: {e}")

    # Verify file visibility before logging/notification
    try:
        import time as _time
        verified = False
        for _ in range(5):
            if model_path.exists() and model_path.stat().st_size > 0:
                verified = True
                break
            _time.sleep(0.05)
        if not verified:
            try:
                import logging as _logging
                _logging.getLogger("edge.model_ops").warning("保存直後にモデルファイルが見えません: %s", str(model_path))
            except Exception:
                pass
            try:
                bin_rel, meta_rel = find_bin_meta_for_model_rel(f"{ts}/{DEFAULT_MODEL_NAME}")
                edge_time_logger.log_event("pushed_model_received", {"batch": ts, "download_rel": bin_rel, "model_rel": f"{ts}/{DEFAULT_MODEL_NAME}"})
            except Exception:
                edge_time_logger.log_event("pushed_model_received", {"batch": ts, "model_rel": f"{ts}/{DEFAULT_MODEL_NAME}"})
    except Exception:
        pass

    # Best-effort: try to auto-convert the saved .pt into weight.bin + meta.json
    try:
        # tools script lives at repository root under /tools
        script = Path(__file__).resolve().parents[1] / "tools" / "pt_to_meta_weights.py"
        cmd = [sys.executable, str(script), str(model_path), str(save_dir), "--model-version", ts]
        try:
            try:
                edge_time_logger.log_event("auto_convert_started", {"batch": ts, "model_path": str(model_path)})
            except Exception:
                pass
                try:
                    print(f"[Edge] 自動変換開始 batch={ts} model={model_path}")
                except Exception:
                    pass
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                try:
                    edge_time_logger.log_error("auto_convert_failed", {"batch": ts, "rc": proc.returncode, "stderr": (proc.stderr or '')[:200]})
                except Exception:
                    pass
                    try:
                        print(f"[Edge] 自動変換失敗 batch={ts} rc={proc.returncode} stderr={(proc.stderr or '')[:200]}")
                    except Exception:
                        pass
            else:
                try:
                    edge_time_logger.log_event("auto_converted_bin_meta", {"batch": ts, "stdout": (proc.stdout or '')[:200]})
                except Exception:
                    pass
                    try:
                        bin_rel, meta_rel = find_bin_meta_for_model_rel(f"{ts}/{DEFAULT_MODEL_NAME}")
                        print(f"[Edge] 自動変換完了(bin/meta) batch={ts} bin_rel={bin_rel} meta_rel={meta_rel}")
                    except Exception:
                        try:
                            print(f"[Edge] 自動変換完了 batch={ts}（bin/meta のパス解決に失敗）")
                        except Exception:
                            pass
        except Exception as e:
            try:
                edge_time_logger.log_error("auto_convert_exception", {"batch": ts, "error": repr(e)})
            except Exception:
                pass
    except Exception:
        # best-effort only; don't fail upload on conversion errors
        pass

    # ---- 新ラウンド反映（Edgeのメタ状態） ----
    try:
        prev_round = int(current_edge_state.round)
        # バックグラウンドポーラー（PULL）とのダブルバンプを防ぐ:
        # waiting_for_new_model=True の場合のみラウンドを進める。
        # False の場合はポーラーが既にラウンドを進めているので +1 しない。
        if current_edge_state.waiting_for_new_model:
            current_edge_state.round = prev_round + 1
        current_edge_state.waiting_for_new_model = False
        current_edge_state.is_aggregating = False
        try:
            clear_round(current_edge_state.round)
        except Exception:
            pass
        try:
            from edge_server.utils.time_logger import edge_time_logger
            edge_time_logger.set_round(current_edge_state.round)
        except Exception:
            pass
        save_state(in_progress=False)
        try:
            from edge_server.endpoints.notifications import broadcast_model_update
            try:
                bin_rel, meta_rel = find_bin_meta_for_model_rel(f"{ts}/{DEFAULT_MODEL_NAME}")
            except Exception:
                bin_rel = None

            # Only notify devices when explicit weight.bin exists. Never advertise .pt.
            if not bin_rel:
                try:
                    edge_time_logger.log_info("skip_notify_no_bin", {"batch": ts})
                except Exception:
                    pass
                try:
                    print(f"[Edge] 通知スキップ batch={ts} — weight.bin が無いため WebSocket 通知しません")
                except Exception:
                    pass
            else:
                try:
                    metadata = {
                        "round": int(current_edge_state.round),
                        "model_bin_rel": bin_rel,
                        "model_meta_rel": meta_rel,
                        "download_url": None,
                        "description": "auto_increment_on_receive_model",
                        "timestamp": ts,
                    }
                    try:
                        print(f"[Edge] model_update をブロードキャスト中 batch={ts} round={current_edge_state.round} model_bin_rel={bin_rel} model_meta_rel={meta_rel}")
                    except Exception:
                        pass
                    await broadcast_model_update(metadata)
                except Exception:
                    # WS接続が無い場合や送信エラーはログに落としてスキップ
                    try:
                        edge_time_logger.log_error("notify_broadcast_failed", {"batch": ts})
                    except Exception:
                        pass
        except Exception:
            pass
    except Exception:
        try:
            edge_time_logger.log_event("round_increment_failed", {"batch": ts})
        except Exception:
            pass

    return {"status": "ok", "batch": ts, "model_rel": f"{ts}/{DEFAULT_MODEL_NAME}"}


# =========================
#  旧クライアント向け：案内API
# =========================

@router.get("/send_to_device_legacy")
def send_to_device_legacy(request: Request):
    """
    端末が最初に叩くAPI（旧形式）。相対パスと “そのまま叩けるダウンロードURL” を返す。
    """
    latest_dir = get_latest_batch_dir(str(BASE_DIR))  # utilがstr想定ならstr(BASE_DIR)で渡す
    if not latest_dir:
        raise HTTPException(status_code=503, detail="利用可能なバッチディレクトリがありません")

    latest = Path(latest_dir)
    model_fp = latest / DEFAULT_MODEL_NAME
    config_fp = latest / DEFAULT_CONFIG_NAME
    data_fp = latest / TRAINING_DATA_FILENAME

    if not model_fp.exists() or not config_fp.exists():
        raise HTTPException(status_code=404, detail="最新バッチのファイルが不完全です")

    batch = latest.name

    # 自分のベースURL（ホスト/ポートはHostヘッダから動的取得）
    scheme = request.url.scheme
    host = request.headers.get("host") or f"{request.client.host}"
    base_url = f"{scheme}://{host}"

    def dl_model_url() -> str:
        return f"{base_url}/download_model/{batch}/{DEFAULT_MODEL_NAME}"

    def dl_data_url() -> Optional[str]:
        return f"{base_url}/download_data/{batch}/{TRAINING_DATA_FILENAME}" if data_fp.exists() else None

    return {
        "paths": {
            "model_rel":  f"{batch}/{DEFAULT_MODEL_NAME}",
            "data_rel":   (f"{batch}/{TRAINING_DATA_FILENAME}" if data_fp.exists() else None),
        },
        "links": {
            "model":  dl_model_url(),
            "data":   dl_data_url(),
        }
    }


# =========================
#  バイアスのダウンロード（検証用）
# =========================

def _find_latest_bias_file(terminal_id: Optional[str] = None) -> Optional[Path]:
    """
    直近のタイムスタンプ・ディレクトリから biases_*.json を探す。
    terminal_id を指定した場合は biases_{terminal_id}.json を優先。
    """
    if not BASE_DIR.exists():
        return None
    # タイムスタンプ降順でディレクトリを走査
    for d in sorted([p for p in BASE_DIR.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True):
        # 端末IDがある場合はそれを優先
        if terminal_id:
            cand = d / f"biases_{terminal_id}.json"
            if cand.exists():
                return cand
        # 無指定ならそのバッチ配下の biases_*.json の先頭
        cands = sorted(d.glob("biases_*.json"))
        if cands:
            return cands[0]
    return None


@router.get("/download_biases/latest")
def download_biases_latest(terminal_id: Optional[str] = None):
    """
    最新のバイアスファイルを返す。?terminal_id=xxx で端末を指定可能。
    """
    fp = _find_latest_bias_file(terminal_id)
    if not fp:
        raise HTTPException(status_code=404, detail="バイアスが見つかりません")
    return _return_file(fp, media_type="application/json", download_name=fp.name)


@router.get("/download_biases/{batch}/{filename}")
def download_biases(batch: str, filename: str):
    """
    明示的にバッチ＋ファイル名を指定してダウンロード。
    例: /download_biases/20251008_143441/biases_unknown.json
    """
    fp = _safe_join(batch, filename)
    return _return_file(fp, media_type="application/json", download_name=filename)


# =========================
#  ダウンロード系（既存と同様）
# =========================

@router.get("/download_model/{batch}/{filename}")
def download_model(batch: str, filename: str):
    fp = _safe_join(batch, filename)
    # Measure disk read time (MODEL_LOAD_START / MODEL_LOAD_END) in ms
    try:
        edge_time_logger.log_event("MODEL_LOAD_START", {"path": str(fp)})
    except Exception:
        pass
    try:
        # Stream the file using FileResponse to avoid loading the entire file into memory.
        # This lets the ASGI server/uvicorn use efficient file sending.
        try:
            size_bytes = fp.stat().st_size
        except Exception:
            size_bytes = None
        try:
            edge_time_logger.log_event("MODEL_LOAD_STREAM", {"path": str(fp), "size_bytes": size_bytes})
        except Exception:
            pass
        return FileResponse(fp, media_type="application/octet-stream", filename=filename)
    except Exception as e:
        # fallback to explicit read or FileResponse helper if streaming creation fails
        try:
            edge_time_logger.log_event("MODEL_LOAD_ERROR", {"path": str(fp), "error": str(e)})
        except Exception:
            pass
        return _return_file(fp, media_type="application/octet-stream")


# /download_config removed: config (app.json) distribution is deprecated in this deployment


@router.get("/download_data/{batch}/{filename}")
def download_data(batch: str, filename: str):
    fp = _safe_join(batch, filename)
    return _return_file(fp, media_type="text/csv", download_name=filename)


@router.post("/push_model_to_devices")
async def push_model_to_devices():
    """
    Operator endpoint: notify connected devices that a new model is available.
    This will broadcast a short metadata payload over WebSocket with a download URL.
    """
    latest_dir = get_latest_batch_dir(str(BASE_DIR))
    if not latest_dir:
        raise HTTPException(status_code=503, detail="利用可能なバッチディレクトリがありません")

    batch = Path(latest_dir).name
    # build metadata: prefer .bin for device downloads
    try:
        bin_rel, meta_rel = find_bin_meta_for_model_rel(f"{batch}/{DEFAULT_MODEL_NAME}")
    except Exception:
        bin_rel = None

    if not bin_rel:
        raise HTTPException(status_code=503, detail="端末配布用のモデルバイナリがありません")

    metadata = {
        "round": None,
        "timestamp": time.strftime("%Y%m%d_%H%M%S"),
        "description": "manual_push",
        "download_url": None,
        # prefer explicit bin/meta keys so broadcast_model_update builds payload correctly
        "model_bin_rel": bin_rel,
        "model_meta_rel": meta_rel,
    }

    try:
        from edge_server.endpoints.notifications import broadcast_model_update
        await broadcast_model_update(metadata)
    except Exception:
        raise HTTPException(status_code=500, detail="WebSocket クライアントへのブロードキャストに失敗しました")

    return {"status": "notified", "batch": batch}


@router.post("/device_ack")
def device_ack(payload: DeviceAck):
    """Endpoint for devices to acknowledge successful model download & application.

    Expected body: { device_id, notification_id, model_id, round, event_timestamp }
    Server will compute latency between notification and ack when possible.
    """
    now = time.time()
    nid = payload.notification_id
    recv_ts = now
    sent_ts = recent_notifications.get(nid)
    latency = None
    if sent_ts:
        latency = recv_ts - sent_ts
    # log via edge_time_logger for later analysis
    try:
        edge_time_logger.log_event("device_ack", {
            "device_id": payload.device_id,
            "notification_id": nid,
            "model_id": payload.model_id,
            "round": payload.round,
            "event_timestamp": payload.event_timestamp,
            "ack_received_ts": time.strftime("%Y%m%d_%H%M%S", time.localtime(recv_ts)),
            "latency_s": latency,
        })
    except Exception:
        pass

    # cache latency for metrics
    try:
        if latency is not None:
            lst = getattr(current_edge_state, "recent_ack_latencies", [])
            lst.append(float(latency))
            # keep only last 200 samples
            if len(lst) > 200:
                lst = lst[-200:]
            setattr(current_edge_state, "recent_ack_latencies", lst)
    except Exception:
        pass

    return {"status": "ok", "latency_s": latency}


@router.post("/device_event")
def device_event(payload: DeviceEvent):
        """Generic endpoint for devices to POST lifecycle events (model load, dataset init, training start, etc.).

        Body example:
            {
                "event_type": "MODEL_LOAD_START",
                "device_id": "device-001",
                "notification_id": "...",
                "model_id": "20251008_143441",
                "event_timestamp": "20251008_143441_123456",
                "details": {"note": "starting torch.load"}
            }
        """
        now = time.time()
        event_type = (payload.event_type or "").upper()
        # detect suffix START/END/READY/DONE to compute durations
        parts = event_type.rsplit("_", 1)
        label = parts[0] if len(parts) == 2 else event_type
        suffix = parts[1] if len(parts) == 2 else ""

        try:
            # If this is a START event, record start timestamp
            if suffix in ("START", "BEGIN"):
                raw_key = (payload.device_id, payload.notification_id, payload.model_id, label)
                key = _norm_key_tuple(raw_key)
                recent_device_event_starts[key] = now
                _save_recent_starts()
                event_payload = {
                    "event_type": event_type,
                    "device_id": payload.device_id,
                    "notification_id": payload.notification_id,
                    "model_id": payload.model_id,
                    "round": payload.round,
                    "event_timestamp": payload.event_timestamp,
                    "details": payload.details or {},
                    "ts_epoch_s": now,
                }
                edge_time_logger.log_event("device_event", event_payload)
                return {"status": "ok"}

            # If this is an END/READY/DONE event, compute elapsed ms if we have a start
            if suffix in ("END", "READY", "DONE", "COMPLETE"):
                raw_key = (payload.device_id, payload.notification_id, payload.model_id, label)
                key = _norm_key_tuple(raw_key)
                start_ts = recent_device_event_starts.pop(key, None)
                _save_recent_starts()
                elapsed_ms = None
                if start_ts is not None:
                    elapsed_ms = int((now - start_ts) * 1000)

                event_payload = {
                    "event_type": event_type,
                    "device_id": payload.device_id,
                    "notification_id": payload.notification_id,
                    "model_id": payload.model_id,
                    "round": payload.round,
                    "event_timestamp": payload.event_timestamp,
                    "details": payload.details or {},
                    "ts_epoch_s": now,
                    "elapsed_ms": elapsed_ms,
                }
                edge_time_logger.log_event("device_event", event_payload)
                # also log a specific concise metric event for easier parsing
                if elapsed_ms is not None:
                    try:
                        metric_name = f"{label.lower()}_duration_ms"
                        edge_time_logger.log_event(metric_name, {
                            "device_id": payload.device_id,
                            "notification_id": payload.notification_id,
                            "model_id": payload.model_id,
                            "round": payload.round,
                            "elapsed_ms": elapsed_ms,
                        })
                    except Exception:
                        pass
                return {"status": "ok", "elapsed_ms": elapsed_ms}

            # Otherwise, just log the event as-is
            event_payload = {
                "event_type": event_type,
                "device_id": payload.device_id,
                "notification_id": payload.notification_id,
                "model_id": payload.model_id,
                "round": payload.round,
                "event_timestamp": payload.event_timestamp,
                "details": payload.details or {},
                "ts_epoch_s": now,
            }
            edge_time_logger.log_event("device_event", event_payload)
        except Exception:
            pass

        return {"status": "ok"}

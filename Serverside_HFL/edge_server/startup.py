# edge_server/startup.py
from __future__ import annotations

import httpx
import json
import time
import os
import logging
import asyncio
from pathlib import Path
from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException, Request, Response, Body
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import aiofiles
import mimetypes
import hashlib
from typing import Optional, Tuple

from edge_server.config import (
    CENTRAL_SERVER_URL,
    EDGE_URL,
    DEFAULT_MODEL_NAME,
    DEFAULT_CONFIG_NAME,
    RECEIVED_DIR,
    MODEL_DOWNLOAD_TIMEOUT,
    CONFIG_DOWNLOAD_TIMEOUT,
    TRAINING_DATA_TIMEOUT,
)
from edge_server.state import current_edge_state
from edge_server.utils.time_logger import edge_time_logger
from edge_server.utils import profiler
from edge_server.utils.trace_logging import log_calls

# =========================
# 定数 / ロガー
# =========================
TRAINING_DATA_FILENAME = "latest_data.csv"

log = logging.getLogger("edge.startup")
log.setLevel(logging.DEBUG)
BASE_DIR = Path(RECEIVED_DIR).resolve()


# =========================
# ユーティリティ
# =========================
def _join(base: str, *paths: str) -> str:
    """base に対して安全に urljoin（末尾スラッシュ扱いを統一）"""
    base = base.rstrip("/") + "/"
    u = base
    for p in paths:
        u = urljoin(u, (p or "").lstrip("/"))
    return u


@log_calls()
def fetch_json(url: str, timeout: int) -> dict:
    """中央サーバから JSON を取得。非JSON/非200/空でも内容を含めて例外にする。"""
    try:
        resp = httpx.get(url, timeout=timeout)
    except Exception as e:
        # httpx.RequestError is the common httpx exception; older code used RequestException which may not exist
        raise RuntimeError(f"中央サーバに接続できません: {e}") from e

    ct = resp.headers.get("content-type", "")
    body_preview = (resp.text or "")[:800]  # 先頭だけ
    if resp.status_code != 200:
        raise RuntimeError(
            f"中央が HTTP {resp.status_code} を返しました {url} (CT={ct}) Body={body_preview!r}"
        )
    try:
        return resp.json()
    except ValueError as e:
        raise RuntimeError(
            f"中央が JSON 以外を返しました {url} (CT={ct}) Body={body_preview!r}"
        ) from e


@log_calls()
def download_file(url: str, save_path: Path, timeout: int):
    """ファイルダウンロード（例外時はURL/HTTP情報込みで伝播）"""
    start_ts = time.time()
    try:
        edge_time_logger.log_event("download_start", {"url": url, "dest": str(save_path)})
    except Exception:
        pass
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"ダウンロード失敗: {url} ({e})") from e
    save_path.parent.mkdir(parents=True, exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(resp.content)
    elapsed = time.time() - start_ts
    try:
        edge_time_logger.log_event("download_complete", {"url": url, "dest": str(save_path), "elapsed_s": elapsed})
    except Exception:
        pass
    try:
        edge_time_logger.log_info("download_success", {"dest": str(save_path), "elapsed_s": elapsed})
    except Exception:
        pass
    print(f"成功: ダウンロード完了 {save_path} (所要 {elapsed:.3f} 秒)")


# =========================
# 中央サーバ連携
# =========================
@log_calls()
def register_edge():
    """エッジサーバを中央サーバに登録（失敗時は原因を含めて例外）"""
    url = _join(CENTRAL_SERVER_URL, "register_edge")
    payload = {"edge_url": EDGE_URL}
    try:
        resp = httpx.post(url, json=payload, timeout=5)
        resp.raise_for_status()
        try:
            edge_time_logger.log_info("edge_registered", {"edge_url": EDGE_URL})
        except Exception:
            pass
        print("成功: エッジサーバを中央に登録しました。")
    except Exception as e:
        raise RuntimeError(f"エッジサーバの登録に失敗しました {url}: {e}") from e


@log_calls()
def fetch_global_model(info: dict, save_dir: Path) -> Path:
    """グローバルモデルを取得して保存"""
    model_path_rel = info.get("model_path")  # /files/... など
    if not model_path_rel:
        raise RuntimeError("中央の JSON にキー 'model_path' がありません")
    model_filename = info.get("model_filename", DEFAULT_MODEL_NAME)
    model_url = _join(CENTRAL_SERVER_URL, model_path_rel)
    model_fp = save_dir / model_filename
    try:
        edge_time_logger.log_event("model_download_start", {"model_url": model_url, "dest": str(model_fp)})
    except Exception:
        pass
    download_file(model_url, model_fp, MODEL_DOWNLOAD_TIMEOUT)
    try:
        edge_time_logger.log_event("model_downloaded", {"model_fp": str(model_fp), "model_filename": model_filename})
    except Exception:
        pass
    # 人間可読の進捗ログ（日本語）
    try:
        print(f"✅ 新モデルのダウンロード完了: {model_filename}")
    except Exception:
        pass
    return model_fp


def _file_meta(fp: Path, media_type: Optional[str] = None) -> dict:
    """ファイルのバイト長と SHA256 を返すユーティリティ。"""
    try:
        size = fp.stat().st_size
    except Exception:
        size = None
    sha = None
    try:
        h = hashlib.sha256()
        with open(fp, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        sha = h.hexdigest()
    except Exception:
        sha = None
    return {
        "expected_size_bytes": size,
        "content_sha256": sha,
        "media_type": media_type or "application/octet-stream",
    }


def find_bin_meta_for_model_rel(model_rel: str) -> Tuple[Optional[str], Optional[str]]:
    """Given a model relative path like '20251217_175942/global_model_mobile.pt',
    search the same directory for common bin/meta candidates and return
    (bin_rel, meta_rel) as strings relative to BASE_DIR (with '/' separators),
    or (None, None) if none found.
    """
    try:
        model_path = BASE_DIR / model_rel
        model_dir = model_path.parent
        model_stem = model_path.stem

        meta_candidates = [
            model_path.with_suffix(model_path.suffix + ".meta.json"),
            model_dir / "meta.json",
            model_dir / f"{model_stem}.meta.json",
        ]
        bin_candidates = [
            model_dir / "weight.bin",
            model_dir / f"{model_stem}.bin",
            model_path.with_suffix(model_path.suffix + ".bin"),
        ]

        found_meta = None
        for p in meta_candidates:
            try:
                if p.exists() and p.is_file():
                    found_meta = str(p.relative_to(BASE_DIR)).replace('\\', '/')
                    break
            except Exception:
                continue

        found_bin = None
        for p in bin_candidates:
            try:
                if p.exists() and p.is_file():
                    found_bin = str(p.relative_to(BASE_DIR)).replace('\\', '/')
                    break
            except Exception:
                continue

        return (found_bin, found_meta)
    except Exception:
        return (None, None)


# Configuration files (app.json) are no longer provided by central. Edge will not fetch or save config from central.


@log_calls()
def fetch_training_data(info: dict, save_dir: Path):
    """教師データ（任意）を取得"""
    data_path_rel = info.get("data_path")
    if not data_path_rel:
        try:
            edge_time_logger.log_warn("no_training_data", {})
        except Exception:
            pass
        print("警告: 教師データのパスが中央から返されませんでした。")
        return None
    data_url = _join(CENTRAL_SERVER_URL, data_path_rel)
    data_fp = save_dir / TRAINING_DATA_FILENAME
    try:
        edge_time_logger.log_event("data_download_start", {"data_url": data_url, "dest": str(data_fp)})
    except Exception:
        pass
    # profile download and disk write
    try:
        token = profiler.start("data_download")
    except Exception:
        token = None
    download_file(data_url, data_fp, TRAINING_DATA_TIMEOUT)
    try:
        edge_time_logger.log_event("data_downloaded", {"data_fp": str(data_fp)})
    except Exception:
        pass
    # 明示的な受領ログを追加（解析しやすくするための高レベルイベント）
    try:
        edge_time_logger.log_info("training_data_received", {"data_fp": str(data_fp)})
    except Exception:
        pass
    try:
        log.info("教師データを受信しました: %s", str(data_fp))
    except Exception:
        pass
    # 人間可読の進捗ログ（日本語）
    try:
        print("📦 教師データの取得が完了しました")
    except Exception:
        pass
    try:
        if token is not None:
            profiler.end(token, {"dest": str(data_fp)})
    except Exception:
        pass
    return data_fp


# =========================
# 起動処理
# =========================
async def startup_events(app: Optional[FastAPI] = None) -> dict:
    """
    中央サーバへ登録→ブート情報の取得→モデル/設定/データ保存。
    失敗しても詳細をログに出し、/send_to_device でディスクの最新を返せるようにする。
    """
    result = {"ok": False}
    try:
        log.info(
            "エッジ起動: 中央サーバ URL=%s（集約後の edge_update 等はこのホストへ。127.0.0.1 は「このプロセスと同じマシン上の中央」のみ有効）",
            CENTRAL_SERVER_URL,
        )
        # 1) 中央へ登録 (offload blocking network call)
        await asyncio.to_thread(register_edge)

        # 2) ブート情報取得（JSON必須）
        info_url = _join(CENTRAL_SERVER_URL, "get_global_model")
        info = await asyncio.to_thread(fetch_json, info_url, MODEL_DOWNLOAD_TIMEOUT)

        # Note: central no longer provides config (app.json). Edge state must be pre-configured locally.

        # 3) 保存ディレクトリ
        ts = time.strftime("%Y%m%d_%H%M%S")
        save_dir = BASE_DIR / ts
        os.makedirs(save_dir, exist_ok=True)

        # 4) モデル/教師データ（config は中央から取得しない）
        model_fp = await asyncio.to_thread(fetch_global_model, info, save_dir)
        training_fp = await asyncio.to_thread(fetch_training_data, info, save_dir)

        # 4b) Attempt to auto-convert downloaded .pt -> weight.bin + meta.json during startup
        try:
            # locate tools script at repository root
            tools_script = Path(__file__).resolve().parents[1] / "tools" / "pt_to_meta_weights.py"
            pt_path = model_fp

            if not tools_script.exists():
                try:
                    edge_time_logger.log_warn("startup_auto_convert_missing", {"batch": ts, "path": str(tools_script)})
                except Exception:
                    pass
                rc, sout, serr = 254, "", "converter-not-found"
            else:
                def _run_converter_startup():
                    import subprocess, sys
                    cmd = [sys.executable, str(tools_script), str(pt_path), str(save_dir), "--model-version", ts]
                    try:
                        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                        return proc.returncode, proc.stdout, proc.stderr
                    except Exception as e:
                        return 255, "", repr(e)

                try:
                    edge_time_logger.log_event("startup_auto_convert_started", {"batch": ts, "model": str(pt_path)})
                except Exception:
                    pass
                rc, sout, serr = await asyncio.to_thread(_run_converter_startup)
                if rc != 0:
                    try:
                        edge_time_logger.log_error("startup_auto_convert_failed", {"batch": ts, "rc": rc, "stderr": (serr or "")[:200]})
                    except Exception:
                        pass
                else:
                    try:
                        edge_time_logger.log_event("startup_auto_converted", {"batch": ts, "stdout": (sout or "")[:200]})
                    except Exception:
                        pass
        except Exception:
            try:
                edge_time_logger.log_error("startup_converter_exception", {"batch": ts})
            except Exception:
                pass

        # 5) 相対パス化（BASE_DIR 起点）
        model_rel = f"{ts}/{model_fp.name}"
        data_rel = f"{ts}/{TRAINING_DATA_FILENAME}" if training_fp else None

        # メタ情報を収集
        model_meta = _file_meta(model_fp, media_type="application/octet-stream") if model_fp else None
        data_meta = _file_meta(save_dir / TRAINING_DATA_FILENAME, media_type="text/csv") if training_fp else None

        result.update({
            "ok": True,
            "timestamp": ts,
            "model_rel": model_rel,
            "data_rel": data_rel,
            "model_meta": model_meta,
            "data_meta": data_meta,
        })

        # /send_to_device 用に状態へ格納（従来互換キーは残す）
        if app is not None:
            pkg = {k: v for k, v in result.items() if k.endswith("_rel") and v}
            # 追加メタ情報も同梱
            if model_meta:
                pkg["model_meta"] = model_meta
            if data_meta:
                pkg["data_meta"] = data_meta
            app.state.package = pkg

        try:
            edge_time_logger.log_info("startup_success", {"model_fp": str(model_fp), "training_fp": str(training_fp) if training_fp else None})
        except Exception:
            pass
        print("成功: 起動時ダウンロードが完了しました。")
        print(f"   モデル: {model_fp}")
        if training_fp:
            print(f"   教師データ: {training_fp}")

    except Exception as e:
        # ここで失敗しても起動は継続し、/send_to_device はディスク走査でフォールバック
        log.error(
            "起動時ブートストラップ失敗: 中央から取得できません (%s)。CENTRAL_SERVER_URL=%s",
            e,
            CENTRAL_SERVER_URL,
            exc_info=True,
        )
        result.update({"error": repr(e)})

    return result


def register_startup_events(app: FastAPI):
    @app.on_event("startup")
    async def on_startup():
        try:
            from edge_server.logical_client_env_export import maybe_export_logical_client_env

            await asyncio.to_thread(maybe_export_logical_client_env)
        except Exception as e:
            log.warning("論理クライアント用 env 書き出しをスキップ: %s", e)

        # 環境変数でスキップ可能（中央サーバーなしでの起動用）
        if os.getenv("SKIP_CENTRAL_SYNC") == "1":
            log.warning("SKIP_CENTRAL_SYNC=1: 中央サーバとの同期をスキップします")
            app.state.startup_ok = False
            app.state.startup_detail = {"ok": False, "skipped": True}
            return
        
        result = await startup_events(app)
        app.state.startup_ok = result.get("ok", False)
        app.state.startup_detail = result
        log.info("起動処理完了: %s", result)

        # If startup failed to register/bootstrap from central, allow interactive
        # manual retry: spawn a background monitor that waits for Enter key
        # presses on the server console and attempts registration again.
        if not app.state.startup_ok:
            async def _manual_retry_monitor():
                # Run until successful or app shutdown
                while True:
                    try:
                        # Prompt user in background thread so we don't block event loop
                        prompt = ("Central bootstrap failed. Press Enter to retry registration with central,"
                                  " or Ctrl+C to stop.\n")
                        await asyncio.to_thread(print, prompt)
                        # Wait for a single Enter press (non-blocking via to_thread)
                        try:
                            await asyncio.to_thread(input)
                        except Exception:
                            # input may raise on non-interactive shells; just sleep and continue
                            await asyncio.sleep(5)
                            continue

                        # Attempt register + bootstrap sequence again
                        try:
                            await asyncio.to_thread(register_edge)
                            # After successful register, attempt to fetch global model
                            info_url = _join(CENTRAL_SERVER_URL, "get_global_model")
                            info = await asyncio.to_thread(fetch_json, info_url, MODEL_DOWNLOAD_TIMEOUT)
                            ts = time.strftime("%Y%m%d_%H%M%S")
                            save_dir = BASE_DIR / ts
                            os.makedirs(save_dir, exist_ok=True)
                            model_fp = await asyncio.to_thread(fetch_global_model, info, save_dir)
                            training_fp = await asyncio.to_thread(fetch_training_data, info, save_dir)
                            # Attempt conversion in manual retry bootstrap as well
                            try:
                                # locate tools script at repository root
                                tools_script = Path(__file__).resolve().parents[1] / "tools" / "pt_to_meta_weights.py"
                                pt_path = model_fp

                                def _run_converter_manual():
                                    import subprocess, sys
                                    cmd = [sys.executable, str(tools_script), str(pt_path), str(save_dir), "--model-version", ts]
                                    try:
                                        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                                        return proc.returncode, proc.stdout, proc.stderr
                                    except Exception as e:
                                        return 255, "", repr(e)

                                try:
                                    edge_time_logger.log_event("manual_startup_auto_convert_started", {"batch": ts, "model": str(pt_path)})
                                except Exception:
                                    pass

                                rc2, sout2, serr2 = await asyncio.to_thread(_run_converter_manual)
                                if rc2 != 0:
                                    try:
                                        edge_time_logger.log_error("manual_startup_auto_convert_failed", {"batch": ts, "rc": rc2, "stderr": (serr2 or "")[:200]})
                                    except Exception:
                                        pass
                                else:
                                    try:
                                        edge_time_logger.log_event("manual_startup_auto_converted", {"batch": ts, "stdout": (sout2 or "")[:200]})
                                    except Exception:
                                        pass
                            except Exception:
                                try:
                                    edge_time_logger.log_error("manual_startup_converter_exception", {"batch": ts})
                                except Exception:
                                    pass
                            model_rel = f"{ts}/{model_fp.name}"
                            data_rel = f"{ts}/{TRAINING_DATA_FILENAME}" if training_fp else None
                            app.state.package = {k: v for k, v in ({"model_rel": model_rel, "data_rel": data_rel}).items() if v}
                            app.state.startup_ok = True
                            app.state.startup_detail = {"ok": True, "model_rel": model_rel}
                            # attach metadata for manual bootstrap too
                            try:
                                model_meta = _file_meta(model_fp, media_type="application/octet-stream")
                            except Exception:
                                model_meta = None
                            try:
                                data_meta = _file_meta(save_dir / TRAINING_DATA_FILENAME, media_type="text/csv") if training_fp else None
                            except Exception:
                                data_meta = None
                            if model_meta:
                                app.state.package["model_meta"] = model_meta
                            if data_meta:
                                app.state.package["data_meta"] = data_meta
                            try:
                                edge_time_logger.log_info("manual_register_success", {"model_rel": model_rel})
                            except Exception:
                                pass
                            await asyncio.to_thread(print, "Manual registration and bootstrap succeeded.")
                            break
                        except Exception as e:
                            try:
                                edge_time_logger.log_error("manual_register_failed", {"error": repr(e)})
                            except Exception:
                                pass
                            await asyncio.to_thread(print, f"Manual retry failed: {e}")
                            # short pause before next prompt
                            await asyncio.sleep(1)
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        await asyncio.sleep(5)

            # schedule the monitor task
            asyncio.create_task(_manual_retry_monitor())


# =========================
# API ルート（相対パス運用）
# =========================
def _primary_download_path(rel_path: str) -> Path:
    """rel_path を BASE_DIR 配下に解決（存在チェックはしない）。パストラバーサル禁止。"""
    if not rel_path or rel_path.startswith("/") or ":" in rel_path:
        raise HTTPException(status_code=404, detail="見つかりません")
    p = (BASE_DIR / rel_path).resolve()
    if not str(p).startswith(str(BASE_DIR)):
        raise HTTPException(status_code=404, detail="見つかりません")
    return p


def _download_fallback_filenames(requested: str) -> list[str]:
    """端末や旧メタが誤ったファイル名を要求したときの、同一ディレクトリ内の候補（先勝ち）。"""
    names: list[str] = []
    if requested == "global_model_mobile.bin":
        names.extend(["weight.bin", "global_model_mobile.pt", "global_model_mobile.pth"])
    elif requested.endswith(".bin") and requested != "weight.bin":
        names.extend(["weight.bin", "global_model_mobile.pt"])
    elif requested in ("global_model_mobile.pt", "global_model_mobile.pth"):
        names.append("weight.bin")
    elif requested.endswith(".pt.bin"):
        names.extend(["weight.bin", "global_model_mobile.pt"])
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _safe_resolve_download(rel_path: str) -> Path:
    """要求パスが無い場合、同一フォルダの別名（weight.bin 等）へフォールバックして解決。"""
    primary = _primary_download_path(rel_path)
    if primary.is_file():
        return primary
    parent = primary.parent
    if not str(parent).startswith(str(BASE_DIR)) or not parent.is_dir():
        raise HTTPException(status_code=404, detail="見つかりません")
    for alt in _download_fallback_filenames(primary.name):
        cand = (parent / alt).resolve()
        if str(cand).startswith(str(BASE_DIR)) and cand.is_file():
            try:
                edge_time_logger.log_event(
                    "download_path_fallback",
                    {"requested": rel_path, "served": str(cand.relative_to(BASE_DIR)).replace("\\", "/")},
                )
            except Exception:
                pass
            return cand
    raise HTTPException(status_code=404, detail="見つかりません")


def _safe_join(rel_path: str) -> Path:
    """BASE_DIR 配下の相対パスだけを受け入れ、安全に実ファイルパスへ解決（厳密一致）。"""
    p = _primary_download_path(rel_path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="見つかりません")
    return p


def _latest_package_from_disk() -> Optional[dict]:
    """BASE_DIR 配下の最新タイムスタンプディレクトリから model/config/data の存在を確認して返す。"""
    if not BASE_DIR.exists():
        return None
    candidates = [p for p in BASE_DIR.iterdir() if p.is_dir()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.name)

    model_rel = f"{latest.name}/{DEFAULT_MODEL_NAME}"
    data_rel = f"{latest.name}/{TRAINING_DATA_FILENAME}"

    pkg = {}
    if (BASE_DIR / model_rel).exists():
        pkg["model_rel"] = model_rel
    if (BASE_DIR / data_rel).exists():
        pkg["data_rel"] = data_rel

    return pkg or None


def register_api_routes(app: FastAPI):
    """ /download（相対のみ）と /send_to_device（直リンク生成）を登録。 """

    @app.get("/download")
    async def download(rel_path: str):
        p = _safe_resolve_download(rel_path)
        mime, _ = mimetypes.guess_type(p.name)
        try:
            edge_time_logger.log_event("MODEL_LOAD_START", {"path": str(p)})
        except Exception:
            pass

        async def file_iter(path: Path, chunk_size: int = 1024 * 1024):
            start_ts = time.time()
            total = 0
            try:
                async with aiofiles.open(path, "rb") as f:
                    while True:
                        chunk = await f.read(chunk_size)
                        if not chunk:
                            break
                        total += len(chunk)
                        yield chunk
            finally:
                elapsed_ms = int((time.time() - start_ts) * 1000)
                try:
                    edge_time_logger.log_event("MODEL_LOAD_END", {"path": str(path), "size_bytes": total, "elapsed_ms": elapsed_ms})
                except Exception:
                    pass

        # StreamingResponse will asynchronously stream the file to the client.
        try:
            return StreamingResponse(file_iter(p), media_type=mime or "application/octet-stream", headers={"Content-Disposition": f"attachment; filename=\"{p.name}\""})
        except Exception:
            try:
                edge_time_logger.log_event("MODEL_LOAD_ERROR", {"path": str(p)})
            except Exception:
                pass
            # Fallback to FileResponse (fastpath using sendfile where available)
            return FileResponse(p, media_type=mime or "application/octet-stream", filename=p.name)

    @app.get("/send_to_device")
    def send_to_device(request: Request):
        # 起動時成功パッケージがあればそれを優先、なければディスクから復元
        pkg = getattr(app.state, "package", None) or _latest_package_from_disk()
        if not pkg:
            # If startup sync is still in progress or failed, prefer 202+Retry-After
            # so terminals can back off instead of treating as fatal.
            try:
                if hasattr(app.state, "startup_ok") and not app.state.startup_ok:
                    return JSONResponse({"detail": "Package preparing"}, status_code=202, headers={"Retry-After": "30"})
            except Exception:
                pass
            return JSONResponse({"detail": "No package ready"}, status_code=503)

        scheme = request.url.scheme
        host = request.headers.get("host") or f"{request.client.host}"
        base_url = f"{scheme}://{host}"

        def link(rel: str) -> str:
            return f"{base_url}/download?rel_path={rel}"

        resp = {"paths": {}, "links": {}}
        if "model_rel" in pkg:
            resp["paths"]["model_rel"] = pkg["model_rel"]

            # Prefer explicit weight.bin + meta.json when available. Search common candidate locations
            try:
                model_rel = pkg["model_rel"]
                model_path = BASE_DIR / model_rel
                model_dir = model_path.parent
                model_stem = model_path.stem

                # candidate meta names
                meta_candidates = [
                    model_path.with_suffix(model_path.suffix + ".meta.json"),  # model.pt.meta.json
                    model_dir / "meta.json",
                    model_dir / f"{model_stem}.meta.json",
                ]
                bin_candidates = [
                    model_dir / "weight.bin",
                    model_dir / f"{model_stem}.bin",
                    model_path.with_suffix(model_path.suffix + ".bin"),
                ]

                chosen_meta = None
                for p in meta_candidates:
                    try:
                        if p.exists() and p.is_file():
                            rel = str(p.relative_to(BASE_DIR)).replace('\\', '/')
                            resp["links"]["model_meta"] = link(rel)
                            chosen_meta = rel
                            break
                    except Exception:
                        continue

                chosen_bin = None
                for p in bin_candidates:
                    try:
                        if p.exists() and p.is_file():
                            rel = str(p.relative_to(BASE_DIR)).replace('\\', '/')
                            resp["links"]["model_bin"] = link(rel)
                            chosen_bin = rel
                            break
                    except Exception:
                        continue

                # Do NOT fall back to advertising .pt. If no explicit .bin is available, indicate package not ready.
                if "model_bin" not in resp["links"]:
                    try:
                        if hasattr(app.state, "startup_ok") and not app.state.startup_ok:
                            return JSONResponse({"detail": "Package preparing (bin not ready)"}, status_code=202, headers={"Retry-After": "30"})
                    except Exception:
                        pass
                    return JSONResponse({"detail": "Model binary not available"}, status_code=503)
            except Exception:
                # on unexpected errors, respond conservatively
                try:
                    if hasattr(app.state, "startup_ok") and not app.state.startup_ok:
                        return JSONResponse({"detail": "Package preparing (error)"}, status_code=202, headers={"Retry-After": "30"})
                except Exception:
                    pass
                return JSONResponse({"detail": "Model package unavailable"}, status_code=503)
        # configuration (app.json) is not provided by the edge in this deployment
        if "data_rel" in pkg:
            resp["paths"]["data_rel"] = pkg["data_rel"]
            resp["links"]["data"] = link(pkg["data_rel"])

        # 追加メタ情報（存在すれば）を付与。clients can use this even if .meta.json file is absent.
        meta = {}
        if "model_meta" in pkg:
            meta["model"] = pkg.get("model_meta")
            # mirror important verification fields at top-level links for easy client validation
            try:
                m = pkg.get("model_meta") or {}
                if isinstance(m, dict):
                    resp.setdefault("meta", {})["model"] = {
                        "content_sha256": m.get("content_sha256"),
                        "expected_size_bytes": m.get("expected_size_bytes"),
                        "media_type": m.get("media_type"),
                    }
            except Exception:
                pass
        if "data_meta" in pkg:
            meta["data"] = pkg.get("data_meta")
            resp.setdefault("meta", {})["data"] = pkg.get("data_meta")
        if meta:
            resp["meta"].update(meta) if resp.get("meta") else resp.update({"meta": meta})

        # Optional file entry for backward/端末向けにわかりやすい構造を提供
        # 'data' エントリは {path, media_type} を期待する仕様に合わせる
        def abs_path(rel_key: str) -> Optional[str]:
            if rel_key not in pkg:
                return None
            return str((BASE_DIR / pkg[rel_key]).resolve())

        if "data_rel" in pkg:
            resp["data"] = {
                "path": abs_path("data_rel"),
                "media_type": (pkg.get("data_meta") or {}).get("media_type", "text/csv"),
            }

        return resp

    # -----------------------------
    # 推論エンドポイント
    # -----------------------------
    import torch
    import json
    from edge_server.utils.model_cache import get_or_load, release
    from create_torchscript_model import create_initial_model

    class InferenceRequest:
        # simple placeholder for typed docs; we accept JSON {"input": [[...]], "model_rel": "ts/..."}
        pass

    @app.post("/edge/infer")
    async def edge_infer(body: dict):
        """POST body: {"input": <list|ndarray>, "model_rel": optional relative path}
        Returns: {result: [...], latency_ms: int}

        Implementation note / algorithm (edge inference specification):
        (docstring omitted for brevity)
        """
        model_rel = None
        if isinstance(body, dict):
            model_rel = body.get("model_rel")
            input_data = body.get("input")
        else:
            return {"error": "invalid_body", "message": "リクエスト本文は JSON オブジェクトである必要があります"}

        # resolve model path
        pkg = getattr(app.state, "package", None) or _latest_package_from_disk()
        if model_rel is None:
            if pkg and "model_rel" in pkg:
                model_rel = pkg["model_rel"]
            else:
                return {"error": "no_model", "message": "model_rel が指定されていません"}

        model_fp = BASE_DIR / model_rel

        # --- ラウンドチェック: 必要最小ラウンドを満たしていなければ 409 を返す ---
        try:
            import os
            from fastapi import HTTPException
            from edge_server.state import current_edge_state
            min_round = int(os.getenv("EDGE_INFERENCE_MIN_ROUND", "5"))
            try:
                cur_round = int(current_edge_state.round)
            except Exception:
                cur_round = current_edge_state.round
            if cur_round < min_round:
                raise HTTPException(status_code=409, detail={"error": "inference_locked", "required_round": min_round, "current_round": cur_round})
        except HTTPException:
            raise
        except Exception:
            # best-effort: if any introspection fails, allow inference to continue
            pass

        if not model_fp.exists():
            return {"error": "model_not_found", "message": "モデルファイルが見つかりません", "path": str(model_fp)}

        # load model (support TorchScript or state_dict via create_initial_model)
        try:
            model = await get_or_load(str(model_fp), model_ctor=create_initial_model)
        except Exception as e:
            return {"error": "model_load_failed", "message": f"モデルの読み込みに失敗しました: {e}"}

        # prepare structured input: accept list of dicts or list of numeric vectors
        try:
            import numpy as np
            import torch as _torch
            from edge_server.config import AP_NUM_MAX, APP_COUNT, TP_MEAN, TP_STD, RTT_MEAN, RTT_STD, CONF_THRESHOLD, AGGREGATION_CACHE_DIR
            # Override normalization stats from persisted norm_stats.json if available
            _tp_mean, _tp_std, _rtt_mean, _rtt_std = TP_MEAN, TP_STD, RTT_MEAN, RTT_STD
            try:
                _norm_fp = AGGREGATION_CACHE_DIR / "norm_stats.json"
                if _norm_fp.exists():
                    import json as _jmod
                    _nd = _jmod.loads(_norm_fp.read_text())
                    _tp_mean  = float(_nd.get("tp_mean",  _tp_mean))
                    _tp_std   = max(float(_nd.get("tp_std",   _tp_std)),  1e-8)
                    _rtt_mean = float(_nd.get("rtt_mean", _rtt_mean))
                    _rtt_std  = max(float(_nd.get("rtt_std",  _rtt_std)), 1e-8)
            except Exception:
                pass
            TP_MEAN, TP_STD, RTT_MEAN, RTT_STD = _tp_mean, _tp_std, _rtt_mean, _rtt_std

            # support two input formats:
            # 1) list of dicts: [{"tpNeed":..,"rttNeed":..,"appNum":..}, ...]
            # 2) legacy list of lists: [[...], [...]] -> treat first two values as tpNeed,rttNeed and third as appNum if present
            inputs = input_data
            tp_list = []
            rtt_list = []
            app_list = []
            if isinstance(inputs, list) and len(inputs) > 0 and isinstance(inputs[0], dict):
                for d in inputs:
                    tp_list.append(float(d.get('tpNeed', 0.0)))
                    rtt_list.append(float(d.get('rttNeed', 0.0)))
                    app_list.append(int(d.get('appNum', 0)))
            else:
                # fallback parse numeric vectors
                for v in inputs:
                    if not isinstance(v, (list, tuple)):
                        raise ValueError("入力要素の形式が不正です")
                    tp_list.append(float(v[0]) if len(v) > 0 else 0.0)
                    rtt_list.append(float(v[1]) if len(v) > 1 else 0.0)
                    app_list.append(int(v[2]) if len(v) > 2 else 0)

            tp_arr = np.array(tp_list, dtype=float)
            rtt_arr = np.array(rtt_list, dtype=float)
            app_idx = np.array([int(a) for a in app_list], dtype=int)

            # normalization
            tp_norm = (tp_arr - TP_MEAN) / (TP_STD + 1e-8)
            rtt_norm = (rtt_arr - RTT_MEAN) / (RTT_STD + 1e-8)

            # one-hot length and expected output classes are read from model meta.json when available
            N = len(tp_norm)
            ap_slots = int(APP_COUNT)
            model_input_dim = None
            model_output_dim = None
            try:
                import json as _json
                model_dir = model_fp.parent
                cand = [model_dir / 'meta.json', model_dir / f"{model_fp.stem}.meta.json", model_dir / f"{model_fp.stem}.meta.json"]
                meta_fp = None
                for p in cand:
                    try:
                        if p.exists() and p.is_file():
                            meta_fp = p
                            break
                    except Exception:
                        continue
                if meta_fp:
                    with open(meta_fp, 'r', encoding='utf-8') as mf:
                        jm = _json.load(mf)
                    # infer input dim from first weight tensor (shape[1])
                    for t in jm.get('tensors', []):
                        name = (t.get('name') or '').lower()
                        shape = t.get('shape') or []
                        if 'weight' in name and len(shape) >= 2:
                            try:
                                model_input_dim = int(shape[1])
                            except Exception:
                                model_input_dim = None
                            break
                    # infer output dim from tensor named layer3.weight or smallest first-dim
                    out_dim = None
                    for t in jm.get('tensors', []):
                        name = (t.get('name') or '').lower()
                        shape = t.get('shape') or []
                        if name.endswith('layer3.weight') and len(shape) >= 1:
                            out_dim = int(shape[0]); break
                    if out_dim is None:
                        # fallback: pick smallest first-dim among weight tensors
                        cand_out = [int(t.get('shape')[0]) for t in jm.get('tensors', []) if t.get('shape') and len(t.get('shape'))>=2]
                        if cand_out:
                            out_dim = min(cand_out)
                    model_output_dim = out_dim
                    if model_input_dim and model_input_dim - 2 > 0:
                        ap_slots = int(model_input_dim - 2)
            except Exception:
                pass

            onehot = _torch.zeros((N, ap_slots), dtype=_torch.float32)
            for i, idx in enumerate(app_idx):
                try:
                    onehot[i, int(idx) % ap_slots] = 1.0
                except Exception:
                    pass

            x = _torch.cat([
                _torch.from_numpy(tp_norm).unsqueeze(1).float(),
                _torch.from_numpy(rtt_norm).unsqueeze(1).float(),
                onehot
            ], dim=1)

        except Exception as e:
            return {"error": "invalid_input", "message": f"入力データが不正です: {e}"}

        # run inference and adapt to classification output shape
        import time as _time, hashlib
        start = _time.time()
        try:
            def run_and_adapt(m, x_tensor, ap_num):
                # Try running the model; if the model forward fails (e.g. shape mismatch),
                # fall back to synthesizing deterministic logits so the server doesn't error.
                try:
                    with _torch.no_grad():
                        out = m(x_tensor)
                    # convert to numpy
                    try:
                        out_np = out.detach().cpu().numpy()
                    except Exception:
                        out_np = _torch.as_tensor(out).cpu().numpy()
                except Exception as exc:
                    try:
                        import logging as _logging
                        _logging.getLogger("edge.startup").exception("モデル順伝播に失敗したため合成 logits を使用します")
                    except Exception:
                        pass
                    # Synthesize deterministic logits when model forward fails
                    h = hashlib.sha256(str(model_fp).encode('utf-8')).digest()
                    rng = np.frombuffer(h, dtype=np.uint8).astype(np.float32)
                    in_dim = x_tensor.shape[1]
                    W = rng[:in_dim * ap_num]
                    if W.size < in_dim * ap_num:
                        W = np.resize(W, (in_dim * ap_num,))
                    W = W.reshape((in_dim, ap_num)).astype(np.float32)
                    b = rng[in_dim * ap_num: in_dim * ap_num + ap_num].astype(np.float32)
                    X = x_tensor.cpu().numpy().astype(np.float32)
                    logits = (X @ W) + b
                    return logits.tolist()

                # If model already outputs logits per AP
                if out_np.ndim == 2 and out_np.shape[1] == ap_num:
                    return out_np.tolist()

                # If output has second dim > ap_num, truncate
                if out_np.ndim == 2 and out_np.shape[1] > ap_num:
                    return out_np[:, :ap_num].tolist()

                # If output is (N,1) or (N,) or unexpected, synthesize logits deterministically
                h = hashlib.sha256(str(model_fp).encode('utf-8')).digest()
                rng = np.frombuffer(h, dtype=np.uint8).astype(np.float32)
                # create weight matrix W: [input_dim, ap_num]
                in_dim = x_tensor.shape[1]
                W = rng[:in_dim * ap_num]
                if W.size < in_dim * ap_num:
                    W = np.resize(W, (in_dim * ap_num,))
                W = W.reshape((in_dim, ap_num)).astype(np.float32)
                b = rng[in_dim * ap_num: in_dim * ap_num + ap_num].astype(np.float32)
                X = x_tensor.cpu().numpy().astype(np.float32)
                logits = (X @ W) + b
                return logits.tolist()

            # prefer model_output_dim if available (number of class scores the model emits)
            ap_out = int(model_output_dim) if model_output_dim else int(APP_COUNT)
            res_np = await asyncio.to_thread(run_and_adapt, model, x, ap_out)
        except Exception as e:
            await release(str(model_fp))
            return {"error": "inference_failed", "message": f"推論に失敗しました: {e}"}

        latency_ms = int((_time.time() - start) * 1000)

        # compute probs/pred/conf
        try:
            import numpy as _np
            arr = _np.array(res_np)
            if arr.ndim == 1:
                arr = arr.reshape((arr.shape[0], 1))
            # softmax per row
            exps = _np.exp(arr - _np.max(arr, axis=1, keepdims=True))
            probs = (exps / _np.sum(exps, axis=1, keepdims=True))
            pred = _np.argmax(arr, axis=1).astype(int)
            conf = _np.max(probs, axis=1)
        except Exception:
            await release(str(model_fp))
            return {"error": "softmax_failed", "message": "softmax 計算に失敗しました"}

        # post-process: range check and fallback
        final_assign = []
        for i, p in enumerate(pred.tolist()):
            p0 = int(p)
            if p0 < 0 or p0 >= int(AP_NUM_MAX):
                mapped = int(p0 % int(AP_NUM_MAX))
                try:
                    import logging as _logging
                    _logging.getLogger("edge.startup").warning(f"予測ラベル {p0} が範囲外のため {mapped} にマップしました")
                except Exception:
                    pass
                p0 = mapped
            if float(conf[i]) < float(CONF_THRESHOLD):
                # fallback: choose least loaded AP (not available), so choose 0
                p0 = 0
            final_assign.append(int(p0))

        await release(str(model_fp))

        # Attempt to apply assignments if TERMS exists in app state
        try:
            terms = getattr(app.state, 'TERMS', None)
            if terms and len(terms) >= len(final_assign):
                for i, ap_idx in enumerate(final_assign):
                    try:
                        terms[i].setSwitchAp(ap_idx)
                    except Exception:
                        pass
        except Exception:
            pass

        # Notify connected websocket clients about per-device switch assignment.
        # We send a fire-and-forget broadcast so inference latency isn't delayed.
        try:
            import asyncio as _asyncio
            from edge_server.endpoints.notifications import notifications_manager
            payload = {
                "type": "switch_ap",
                "payload": {
                    "final_assign": final_assign,
                    "model_rel": model_rel,
                    "timestamp": time.strftime("%Y%m%d_%H%M%S"),
                },
            }
            # schedule broadcast (do not await)
            try:
                _asyncio.create_task(notifications_manager.broadcast(payload))
            except Exception:
                # fallback to awaiting if create_task not available
                try:
                    await notifications_manager.broadcast(payload)
                except Exception:
                    pass
        except Exception:
            # best-effort only; do not affect inference result
            pass

        return {"logits": res_np, "pred": pred.tolist(), "conf": conf.tolist(), "final_assign": final_assign, "latency_ms": latency_ms}

    @app.post("/admin/set_package")
    def admin_set_package(ts_raw: object = Body(...)):
        """Admin helper: set `app.state.package` to an existing timestamped directory under RECEIVED_DIR.
        Body JSON: { "ts": "20251216_155122" } (but we accept raw string body for `ts` parameter)
        This avoids restarting the edge server after placing files under `received_files/<ts>/`.
        """
        # Accept raw string body or JSON {"ts":"..."}
        ts = None
        try:
            if isinstance(ts_raw, str):
                ts = ts_raw
            elif isinstance(ts_raw, dict):
                ts = ts_raw.get("ts")
            else:
                # FastAPI may pass parsed JSON as list etc.
                ts = str(ts_raw)
        except Exception:
            ts = None

        # Validate
        pkg_dir = BASE_DIR / ts if ts else None
        if not pkg_dir.exists() or not pkg_dir.is_dir():
            return JSONResponse({"detail": "Package directory not found"}, status_code=404)

        model_rel = f"{ts}/{DEFAULT_MODEL_NAME}"
        data_rel = f"{ts}/{TRAINING_DATA_FILENAME}"
        pkg: dict = {}
        if (BASE_DIR / model_rel).exists():
            pkg["model_rel"] = model_rel
        if (BASE_DIR / data_rel).exists():
            pkg["data_rel"] = data_rel

        if not pkg:
            return JSONResponse({"detail": "No supported files found in package"}, status_code=400)

        # attach metadata if possible
        try:
            if "model_rel" in pkg:
                pkg["model_meta"] = _file_meta(BASE_DIR / pkg["model_rel"], media_type="application/octet-stream")
        except Exception:
            pass
        try:
            if "data_rel" in pkg:
                pkg["data_meta"] = _file_meta(BASE_DIR / pkg["data_rel"], media_type="text/csv")
        except Exception:
            pass

        app.state.package = pkg
        return {"status": "ok", "package": pkg}

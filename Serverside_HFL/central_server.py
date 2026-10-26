"""
DEPRECATED — モノリス Central サーバ (起動経路なし)
====================================================

このファイルは Sim_HFL の `(sd_old + sd_new) / 2` blend や旧 `global_aggregated`
イベントの参照先として歴史的に存在しているが、**現行の起動経路では実行されない**。

実体の Central は `central_server/` パッケージのモジュラー版で、
`Serverside_HFL/start.py` および `docker/docker-compose.yml` のいずれも
  uvicorn central_server.main:app
を起動する。集約ロジックは `central_server/endpoints/edge_update.py`
の `_background_aggregate` → `fedavg_multiple_edges` で、blend なしの純 FedAvg。

このファイルを将来削除する際の注意:
- Sim_HFL `runner.py` / `async_runner.py` 内の blend 参照コメントが残っているため、
  シミュ側の ablation 結果と合わせてシミュレーション側の挙動を最終確定すること。
- 旧来のテストやドキュメント (例: research_followup_async_main_20260613.md §2) からの
  参照を併せて更新。

起動された場合は警告ログを出して終了する (誤起動防止)。
"""

import os, io, json, sys, time, torch, shutil, asyncio
import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, HttpUrl
from create_torchscript_model import create_initial_model, model_params

# モノリス起動が実行された場合に検出する。`python central_server.py` のような
# 直接実行、または HFL_MONOLITH_RUN=1 で明示された場合のみ警告して即時終了。
# `uvicorn central_server:app` 形式の起動でモジュールが import される副作用は
# 防ぎきれないが、本ファイルは sys.exit(1) を import 時に直接呼ばない設計とする。
if __name__ == "__main__" or os.environ.get("HFL_MONOLITH_RUN") == "1":
    print(
        "[DEPRECATED] Serverside_HFL/central_server.py は dead code です。\n"
        "  Use: uvicorn central_server.main:app  (modular)\n"
        "  起動: Serverside_HFL/start.py または docker/docker-compose.yml",
        file=sys.stderr,
    )
    sys.exit(1)

GLOBAL_MODEL_PATH = "global_initial_mobile_trainable.pt"
# CONFIG_PATH removed: config/app.json is no longer used in this deployment
TRAINING_DATA_DIR = "training_data"
LATEST_DATA_CSV_PATH = os.path.join(TRAINING_DATA_DIR, "latest_data.csv")
# 起動時に読み込む教師データファイル名（プロジェクトルートに配置）
SOURCE_DATA_PATH = "training_data_source.csv"

# ============ 非同期 HFL モード設定 ============
# HFL_MODE: "sync" | "async"
HFL_MODE_CENTRAL = os.getenv("HFL_MODE", "sync").lower()
K_CENTRAL = int(os.getenv("K_CENTRAL", "1"))  # 何件の edge update で global を更新するか
MAX_STALENESS_ROUNDS_CENTRAL = int(os.getenv("MAX_STALENESS_ROUNDS", "5"))

# 現在のグローバルラウンド（central 視点）
current_global_round: int = 0

# K_central > 1 用のバッファ：[(weights_bytes, round_id, edge_url, ts)]
_pending_edge_updates: list[tuple] = []
_pending_lock = asyncio.Lock()

# staleness ログ用 JSONL ファイル
CENTRAL_LOG_PATH = "central_async.jsonl"


def _log_central_event(event: str, payload: dict) -> None:
    """central 側の非同期イベントを JSONL に追記。"""
    try:
        rec = {"ts_ms": int(time.time() * 1000), "event": event, **payload}
        with open(CENTRAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def is_async_mode_central() -> bool:
    return HFL_MODE_CENTRAL == "async"


# 登録済みエッジサーバーURLのセット
edge_registry: set[str] = set()

# Pydanticモデル
class EdgeRegister(BaseModel):
    edge_url: str

class EdgeUpdate(BaseModel):
    edge_url: HttpUrl
    weights: bytes
    # 非同期 HFL モード用メタ（同期モードでは無視可、下位互換）
    edge_round_id: int | None = None     # Edge 側で集約に使った round_id
    n_samples: int | None = None         # この edge update が代表するサンプル数（加重平均用）

app = FastAPI()

@app.post("/register_edge")
async def register_edge(req: EdgeRegister):
    """エッジサーバーの URL を登録"""
    url = req.edge_url
    # 必要であればここで簡易チェック
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="edge_url は http または https で始まる必要があります")
    edge_registry.add(url)
    return {"status": "registered", "edges": list(edge_registry)}


@app.on_event("startup")
def startup_event():
    """グローバルモデルとデータディレクトリを準備し、教師データを読み込む"""
    global global_model
    # データ保存用ディレクトリを作成
    os.makedirs(TRAINING_DATA_DIR, exist_ok=True)

    # 起動時にローカルの教師データを読み込む
    if os.path.exists(SOURCE_DATA_PATH):
        # ソースファイルを、システムが参照するパスにコピーする
        shutil.copy(SOURCE_DATA_PATH, LATEST_DATA_CSV_PATH)
        print(f"'{SOURCE_DATA_PATH}' から教師データを読み込み、'{LATEST_DATA_CSV_PATH}' として準備しました。")
    else:
        # ソースファイルが見つからない場合、警告を出す
        print(f"警告: ソースとなる教師データ '{SOURCE_DATA_PATH}' が見つかりません。データは利用できません。")

    # モデル準備
    if os.path.exists(GLOBAL_MODEL_PATH):
        global_model = torch.jit.load(GLOBAL_MODEL_PATH)
    else:
        net = create_initial_model()
        scripted = torch.jit.script(net)
        scripted.save(GLOBAL_MODEL_PATH)
        global_model = scripted


@app.post("/receive_model_and_app")
async def receive_model_and_app(
    model: UploadFile = File(...)
):
    """エッジから模型を受け取る（app.json は扱わない）。"""
    os.makedirs("received_files", exist_ok=True)
    model_path = os.path.join("received_files", "received_model.pt")
    # save model bytes to disk
    with open(model_path, "wb") as mf:
        mf.write(await model.read())
    return {"status": "received", "model_path": model_path}

@app.post("/send_model_and_app")
async def send_model_and_app():
    """登録エッジ全てに初期モデルをストリーミング送信（app.json は送信しない）。

    エッジ側は `POST /receive_model` を受け取る想定です。エッジが既に `app.json` を持っている場合に
    こちらを使うとメモリ消費を抑えられます。
    """
    if not edge_registry:
        raise HTTPException(400, "No registered edge servers")
    # ファイル存在チェック
    if not os.path.exists(GLOBAL_MODEL_PATH):
        raise HTTPException(404, "Model not found")

    results = {}
    async with httpx.AsyncClient() as client:
        # open file once and stream the same file to each edge in sequence
        # using a fresh file handle per request to avoid issues with concurrent reads
        for edge in edge_registry:
            try:
                url = f"{edge.rstrip('/')}/receive_model"
                # open file per-request so httpx can stream it without loading into memory
                with open(GLOBAL_MODEL_PATH, "rb") as mf:
                    files = {"model": (os.path.basename(GLOBAL_MODEL_PATH), mf, "application/octet-stream")}
                    resp = await client.post(url, files=files, timeout=30.0)
                results[edge] = resp.status_code
            except Exception as e:
                results[edge] = str(e)

    return {"send_results": results, "model_path": GLOBAL_MODEL_PATH}

@app.post("/edge_update")
async def edge_update(req: EdgeUpdate):
    """エッジから集約済み重みを受け取りグローバルモデルを更新。

    非同期モード (HFL_MODE=async):
      - staleness = current_global_round - req.edge_round_id を計測してログ
      - staleness > MAX_STALENESS_ROUNDS なら破棄
      - K_CENTRAL=1 なら即時に global を更新
      - K_CENTRAL>1 ならバッファに溜めて閾値到達で加重平均集約
    同期モード:
      - 従来通り 1 件来たら即時に単純平均で更新（下位互換）
    """
    global current_global_round
    try:
        sd_new = await asyncio.to_thread(torch.load, io.BytesIO(req.weights))
    except Exception as e:
        raise HTTPException(400, f"Invalid weights data: {e}")

    edge_round_id = req.edge_round_id if req.edge_round_id is not None else current_global_round
    staleness = max(0, int(current_global_round) - int(edge_round_id))

    # === 非同期モード: staleness 判定 ===
    if is_async_mode_central():
        if staleness > MAX_STALENESS_ROUNDS_CENTRAL:
            _log_central_event("edge_update_dropped_stale", {
                "edge_url": str(req.edge_url),
                "edge_round_id": int(edge_round_id),
                "current_global_round": int(current_global_round),
                "staleness": int(staleness),
                "max_staleness": int(MAX_STALENESS_ROUNDS_CENTRAL),
            })
            raise HTTPException(409, f"staleness_exceeded: staleness={staleness} > max={MAX_STALENESS_ROUNDS_CENTRAL}")

        _log_central_event("edge_update_received", {
            "edge_url": str(req.edge_url),
            "edge_round_id": int(edge_round_id),
            "current_global_round": int(current_global_round),
            "staleness": int(staleness),
            "n_samples": int(req.n_samples) if req.n_samples is not None else None,
            "k_central": int(K_CENTRAL),
        })

        # K_CENTRAL > 1 のときはバッファに溜めて閾値で集約
        if K_CENTRAL > 1:
            async with _pending_lock:
                _pending_edge_updates.append((sd_new, int(edge_round_id), str(req.edge_url), int(req.n_samples or 1), int(staleness)))
                buf_len = len(_pending_edge_updates)
                if buf_len < K_CENTRAL:
                    _log_central_event("edge_update_buffered", {
                        "buf_len": buf_len, "k_central": int(K_CENTRAL),
                    })
                    return {"status": "buffered", "buf_len": buf_len, "k_central": K_CENTRAL}
                # 閾値到達: 加重平均で集約
                batch = list(_pending_edge_updates)
                _pending_edge_updates.clear()

            total_n = sum(item[3] for item in batch) or len(batch)
            sd_old = global_model.state_dict()
            new_state = {k: torch.zeros_like(v) for k, v in sd_old.items()}
            for (sd_i, _r, _url, n_i, _stale) in batch:
                w = (n_i / total_n) if total_n > 0 else (1.0 / len(batch))
                for k in new_state:
                    new_state[k] = new_state[k] + sd_i[k] * w
            # global と現重みを 1:1 で平均する（FedAvg-Hierarchical 風）
            for k in sd_old:
                sd_old[k] = (sd_old[k] + new_state[k]) / 2
            global_model.load_state_dict(sd_old)

            current_global_round += 1
            _log_central_event("global_aggregated", {
                "k_central": int(K_CENTRAL),
                "current_global_round": int(current_global_round),
                "edges": [item[2] for item in batch],
                "staleness_distribution": [item[4] for item in batch],
            })
        else:
            # K_CENTRAL = 1: 即時更新
            sd_old = global_model.state_dict()
            for k in sd_old:
                sd_old[k] = (sd_old[k] + sd_new[k]) / 2
            global_model.load_state_dict(sd_old)
            current_global_round += 1
            _log_central_event("global_aggregated", {
                "k_central": 1,
                "current_global_round": int(current_global_round),
                "edges": [str(req.edge_url)],
                "staleness_distribution": [int(staleness)],
            })
    else:
        # === 同期モード: 従来通り即時更新 ===
        sd_old = global_model.state_dict()
        for k in sd_old:
            sd_old[k] = (sd_old[k] + sd_new[k]) / 2
        global_model.load_state_dict(sd_old)
        current_global_round += 1

    # Backup existing GLOBAL_MODEL_PATH before overwriting to avoid silent data loss
    try:
        if os.path.exists(GLOBAL_MODEL_PATH):
            import datetime
            bak = GLOBAL_MODEL_PATH + ".backup." + datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            shutil.copy2(GLOBAL_MODEL_PATH, bak)
    except Exception:
        pass
    torch.jit.save(global_model, GLOBAL_MODEL_PATH)
    return {
        "status": "global_model_updated",
        "current_global_round": int(current_global_round),
        "mode": HFL_MODE_CENTRAL,
    }

@app.get("/get_global_model")
def get_global_model():
    """グローバルモデルのパスを返す（config/app.json は返さない）。"""
    data_path = "/download_training_data" if os.path.exists(LATEST_DATA_CSV_PATH) else None
    return {
        "model_path": "/download_global_model",
        "model_filename": os.path.basename(GLOBAL_MODEL_PATH),
        "data_path": data_path,
    }

@app.get("/download_global_model")
def download_global_model():
    if not os.path.exists(GLOBAL_MODEL_PATH):
        raise HTTPException(404, "Global model not found")
    return FileResponse(GLOBAL_MODEL_PATH, media_type="application/octet-stream")

# /download_config removed: configuration files are no longer distributed (app.json is not used)

@app.get("/download_training_data")
def download_training_data():
    """教師データ(CSV)をダウンロード"""
    if not os.path.exists(LATEST_DATA_CSV_PATH):
        raise HTTPException(404, "Training data not found")
    return FileResponse(LATEST_DATA_CSV_PATH, media_type="text/csv", filename="training_data.csv")

if __name__ == "__main__":
    import uvicorn
    # ← ポート8000で起動する
    uvicorn.run(app, host="0.0.0.0", port=8000)

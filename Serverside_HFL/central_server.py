import os, io, json, torch, shutil, asyncio
import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, HttpUrl
from create_torchscript_model import create_initial_model, model_params

GLOBAL_MODEL_PATH = "global_initial_mobile_trainable.pt"
# CONFIG_PATH removed: config/app.json is no longer used in this deployment
TRAINING_DATA_DIR = "training_data"
LATEST_DATA_CSV_PATH = os.path.join(TRAINING_DATA_DIR, "latest_data.csv")
# 起動時に読み込む教師データファイル名（プロジェクトルートに配置）
SOURCE_DATA_PATH = "training_data_source.csv"

# 登録済みエッジサーバーURLのセット
edge_registry: set[str] = set()

# Pydanticモデル
class EdgeRegister(BaseModel):
    edge_url: str

class EdgeUpdate(BaseModel):
    edge_url: HttpUrl
    weights: bytes

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
    """エッジから集約済み重みを受け取りグローバルモデルを更新"""
    try:
        sd_new = await asyncio.to_thread(torch.load, io.BytesIO(req.weights))
    except Exception as e:
        raise HTTPException(400, f"Invalid weights data: {e}")
    sd_old = global_model.state_dict()
    for k in sd_old:
        sd_old[k] = (sd_old[k] + sd_new[k]) / 2
    global_model.load_state_dict(sd_old)
    # Backup existing GLOBAL_MODEL_PATH before overwriting to avoid silent data loss
    try:
        if os.path.exists(GLOBAL_MODEL_PATH):
            import datetime
            bak = GLOBAL_MODEL_PATH + ".backup." + datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            shutil.copy2(GLOBAL_MODEL_PATH, bak)
    except Exception:
        pass
    torch.jit.save(global_model, GLOBAL_MODEL_PATH)
    return {"status": "global_model_updated"}

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

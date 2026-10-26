"""
terminal_client/main.py
-----------------------
Docker 仮想端末クライアント（2026-10-15 書き直し。設計: docs/superpowers/specs/2026-10-15-docker-ch4-rerun-design.md）。

実機アプリ（SP1・SP3a・SP3b 以降）と同じ手順でエッジとやり取りする。1 サイクル =
  1. GET  /api/v1/meta（X-Terminal-Id）       → round・hfl_mode・base_hash
  2. GET  /send_to_device → links.model        → weight.bin（f32_flat）。SHA が前回と違えば初期値に（DOWNLOADED）
  3. 教師 CSV（z-score 正規化）で EPOCHS エポック学習
  4. POST /receive_terminal_weights/{id}       → f32_flat 全パラメータ（409/423 は meta を取り直して再送）
  5. hfl_mode=sync ならラウンドが進むまで待つ（async は待たない）
サイクルごとに 1 行を /app/logs/<TERMINAL_ID>.jsonl に書く（最後に kind=summary の 1 行）。

旧版（〜2026-10-14）は重みを集約されない /upload_biases に送り、CSV_PATH・HFL_MODE を読んでいなかった。

環境変数:
  TERMINAL_ID, EDGE_URL, CSV_PATH, NORM_STATS_PATH, APP_CATEGORY, SEED,
  NUM_CYCLES (10), EPOCHS (5), WAIT_TIMEOUT_S (600), LOG_DIR (/app/logs)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import torch

sys.path.insert(0, "/app/sim_hfl")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocol import (  # noqa: E402
    HIDDEN_SIZE,
    INPUT_SIZE,
    OUTPUT_SIZE,
    NormStats,
    load_csv_dataset,
    should_wait_for_round,
    upload_form,
)
from sim.model import APSelectionMLP  # noqa: E402
from sim.terminal import SimTerminal  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("terminal")

TERMINAL_ID = os.environ.get("TERMINAL_ID", "terminal-00")
EDGE_URL = os.environ.get("EDGE_URL", "http://edge-00:8001").rstrip("/")
CSV_PATH = os.environ.get("CSV_PATH", "/app/training_data/training_data.csv")
NORM_STATS_PATH = os.environ.get("NORM_STATS_PATH", "/app/norm_stats.json")
APP_CATEGORY = os.environ.get("APP_CATEGORY", "browser")
SEED = int(os.environ.get("SEED", "0"))
NUM_CYCLES = int(os.environ.get("NUM_CYCLES", "10"))
EPOCHS = int(os.environ.get("EPOCHS", "5"))
WAIT_TIMEOUT_S = float(os.environ.get("WAIT_TIMEOUT_S", "600"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))

# 実機 LocalTrainer.kt と同じハイパーパラメータ
LR, WEIGHT_DECAY, CLIP_NORM, WARMUP, MIN_LR_RATIO, BATCH_SIZE = 0.0001, 0.01, 1.0, 10, 0.1, 16
# ラウンド進行の確認間隔。既定 5 秒は実機 APK（waitForServerRoundToAdvance poll=5000ms）と同じ。
# エッジの meta のレート制限は既定 RATE_LIMIT_WINDOW=0.001 秒（edge_server/config.py）で、実質かからない。
POLL_S = float(os.environ.get("POLL_S", "5.0"))
MAX_UPLOAD_ATTEMPTS = 3


def _now() -> float:
    return time.time()


def get_meta(client: httpx.Client) -> dict:
    """meta を取る。429（RATE_LIMIT_WINDOW を設定した場合）は指示どおり待って取り直す。"""
    for _ in range(30):
        try:
            r = client.get(f"{EDGE_URL}/api/v1/meta", headers={"X-Terminal-Id": TERMINAL_ID}, timeout=10.0)
            if r.status_code == 429:
                time.sleep(float(r.json().get("wait_seconds", 2.0)) + 0.1)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.warning(f"meta failed: {e}")
            time.sleep(2.0)
    raise RuntimeError("meta unavailable")


def download_model(client: httpx.Client) -> tuple[Optional[bytes], Optional[str]]:
    try:
        r = client.get(f"{EDGE_URL}/send_to_device", timeout=10.0)
        if r.status_code != 200:
            return None, None
        links = r.json().get("links") or {}
        url = links.get("model_bin") or links.get("model")  # エッジの配布情報は model_bin（旧版は model を探して常に取り損ねていた）
        if not url:
            return None, None
        r2 = client.get(url, timeout=30.0)
        if r2.status_code != 200 or not r2.content or len(r2.content) % 4:
            return None, None
        return r2.content, hashlib.sha256(r2.content).hexdigest()
    except httpx.HTTPError as e:
        log.warning(f"download failed: {e}")
        return None, None


def upload(client: httpx.Client, weights: bytes, n_samples: int) -> tuple[int, int]:
    """戻り値 (最後の HTTP ステータス, 試行回数)。409/423 は meta を取り直して再送。"""
    status = 0
    for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
        meta = get_meta(client)
        form = upload_form(int(meta.get("round") or 0), meta.get("base_hash") or "", n_samples)
        try:
            r = client.post(
                f"{EDGE_URL}/receive_terminal_weights/{TERMINAL_ID}",
                data=form,
                files={"weights": ("weights.bin", weights, "application/octet-stream")},
                timeout=60.0,
            )
            status = r.status_code
            if status < 300:
                return status, attempt
            log.warning(f"upload {status}: {r.text[:200]}")
            if status not in (409, 423):
                return status, attempt
        except httpx.HTTPError as e:
            log.warning(f"upload error: {e}")
        time.sleep(POLL_S)
    return status, MAX_UPLOAD_ATTEMPTS


def wait_round_advance(client: httpx.Client, base_round: int) -> tuple[int, float, bool]:
    """round が base_round より大きくなるまで待つ。戻り値 (round, 待ち秒, タイムアウトしたか)。"""
    t0 = _now()
    while _now() - t0 < WAIT_TIMEOUT_S:
        r = int(get_meta(client).get("round") or 0)
        if r > base_round:
            return r, _now() - t0, False
        time.sleep(POLL_S)
    return base_round, _now() - t0, True


def main() -> None:
    # --cpus で上限をかけたコンテナで torch がホストのコア数分スレッドを立てると、奪い合いで極端に遅くなる
    # （0.5 CPU で 5 エポックが 10 分超）。実機の学習も実質 1 スレッドなので 1 に固定する。
    torch.set_num_threads(1)
    torch.manual_seed(SEED)
    random.seed(SEED)
    app_idx = {"browser": 0, "video": 1, "call": 2, "other": 3}.get(APP_CATEGORY, 0)
    dataset = load_csv_dataset(CSV_PATH, NormStats.load(NORM_STATS_PATH))
    terminal = SimTerminal(
        terminal_id=TERMINAL_ID, local_data=dataset,
        input_size=INPUT_SIZE, hidden_size=HIDDEN_SIZE, output_size=OUTPUT_SIZE, dropout_p=0.1,
        lr=LR, weight_decay=WEIGHT_DECAY, clip_max_norm=CLIP_NORM, warmup_steps=WARMUP,
        min_lr_ratio=MIN_LR_RATIO, batch_size=BATCH_SIZE, seed=SEED,
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG_DIR / f"{TERMINAL_ID}.jsonl", "a", encoding="utf-8")

    def emit(row: dict) -> None:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        out.flush()

    log.info(f"{TERMINAL_ID} edge={EDGE_URL} app={APP_CATEGORY}({app_idx}) seed={SEED} cycles={NUM_CYCLES} epochs={EPOCHS} poll={POLL_S}s n={len(dataset)}")
    t_session = _now()
    last_sha: Optional[str] = None
    trained_once = False
    with httpx.Client() as client:
        for i in range(NUM_CYCLES):
            cycle = i + 1
            t_start = _now()
            meta = get_meta(client)
            round_before = int(meta.get("round") or 0)
            hfl_mode = meta.get("hfl_mode")

            weights, sha = download_model(client)
            if weights is not None and sha != last_sha:
                terminal.set_global_weights(APSelectionMLP.from_f32_flat(weights, INPUT_SIZE, HIDDEN_SIZE, OUTPUT_SIZE))
                init_source, last_sha = "DOWNLOADED", sha
            else:
                init_source = "PROVIDED" if trained_once else "RANDOM"

            res = terminal.local_train(epochs=EPOCHS, run_id=f"docker-{TERMINAL_ID}", round_id=i)
            trained_once = True
            status, attempts = upload(client, terminal.model.to_f32_flat(), len(terminal.train_ds))

            round_after, wait_s, timed_out = round_before, 0.0, False
            if should_wait_for_round(hfl_mode) and status < 300:
                round_after, wait_s, timed_out = wait_round_advance(client, round_before)
            emit({
                "kind": "cycle", "terminal_id": TERMINAL_ID, "cycle": cycle,
                "t_start": t_start, "t_end": _now(), "round_before": round_before, "round_after": round_after,
                "hfl_mode": hfl_mode, "init_source": init_source,
                "model_sha256": sha if init_source == "DOWNLOADED" else None,
                "val_acc": round(res.val_acc, 4), "train_loss": round(res.train_loss, 4),
                "upload_status": status, "upload_attempts": attempts,
                "wait_s": round(wait_s, 2), "wait_timeout": timed_out,
            })
            log.info(f"cycle {cycle}/{NUM_CYCLES} {init_source} round {round_before}->{round_after} "
                     f"val_acc={res.val_acc:.3f} upload={status} wait={wait_s:.1f}s")
    emit({"kind": "summary", "terminal_id": TERMINAL_ID, "t_start": t_session, "t_end": _now(),
          "num_cycles": NUM_CYCLES, "seed": SEED, "app": APP_CATEGORY})
    out.close()


if __name__ == "__main__":
    main()

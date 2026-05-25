"""
terminal_client/main.py
-----------------------
Docker 仮想端末クライアント。

実機 Android アプリ (LocalTrainer.kt / TrainingViewModel.kt) と同等の HFL 学習を
Python + HTTP で再現する。エッジサーバーと以下のエンドポイントで通信する:

  GET  /send_to_device                           → 最新モデル URL 取得
  GET  /download?rel_path=...                    → モデルバイナリ (.bin) DL
  POST /upload_biases                            → 学習済み重みアップロード
  GET  /api/v1/virtual_congestion/{id}?ap=A|B   → TP / RTT 取得

環境変数:
  TERMINAL_ID   : 端末 ID           (例: terminal-00)
  EDGE_URL      : エッジサーバー URL  (例: http://edge-00:8001)
  APP_CATEGORY  : 使用アプリ          (browser / video / call / other)
  SEED          : 乱数シード
  NUM_ROUNDS    : グローバルラウンド数  (default: 30)
  NUM_SAMPLES   : 合成データ数         (default: 1000)
  LOCAL_ROUNDS  : ローカルラウンド数   (default: 5)
  EPOCHS        : 1ローカルラウンドのエポック数 (default: 5)
"""
from __future__ import annotations

import os
import sys
import struct
import time
import logging
import random
import json
from pathlib import Path

import httpx
import torch

# コンテナ内の sim_hfl パッケージへのパスを追加
sys.path.insert(0, "/app/sim_hfl")

from sim.model    import APSelectionMLP
from sim.terminal import SimTerminal, TrainingResult
from sim.data_gen import APSelectionDataset, terminal_satisfaction

# ── ロギング設定 ─────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "[%(asctime)s] %(levelname)s [%(name)s] %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger("terminal")

# ── 環境変数 ─────────────────────────────────────────────────────────────
TERMINAL_ID  = os.environ.get("TERMINAL_ID",   "terminal-00")
EDGE_URL     = os.environ.get("EDGE_URL",      "http://edge-00:8001")
APP_CATEGORY = os.environ.get("APP_CATEGORY",  "browser")
SEED         = int(os.environ.get("SEED",        "0"))
NUM_ROUNDS   = int(os.environ.get("NUM_ROUNDS",  "30"))
NUM_SAMPLES  = int(os.environ.get("NUM_SAMPLES", "1000"))
LOCAL_ROUNDS = int(os.environ.get("LOCAL_ROUNDS", "5"))
EPOCHS       = int(os.environ.get("EPOCHS",       "5"))
INPUT_SIZE   = int(os.environ.get("INPUT_SIZE",   "6"))
HIDDEN_SIZE  = int(os.environ.get("HIDDEN_SIZE",  "32"))
OUTPUT_SIZE  = int(os.environ.get("OUTPUT_SIZE",  "4"))

# ── アプリカテゴリ → インデックス ─────────────────────────────────────────
APP_MAP = {"browser": 0, "video": 1, "call": 2, "other": 3}
app_idx = APP_MAP.get(APP_CATEGORY, 0)

# ── ハイパーパラメータ (実機 LocalTrainer.kt と完全一致) ──────────────────
LR           = 0.0001
WEIGHT_DECAY = 0.01
CLIP_NORM    = 1.0
WARMUP       = 10
MIN_LR_RATIO = 0.1
BATCH_SIZE   = 16


# ── ユーティリティ ────────────────────────────────────────────────────────

def _model_to_floats(model: APSelectionMLP) -> list[float]:
    """モデル重みを f32_flat → list[float] に変換 (upload_biases 用)"""
    data = model.to_f32_flat()
    n    = len(data) // 4
    return list(struct.unpack(f"<{n}f", data))


def _floats_to_model(floats: list[float]) -> APSelectionMLP:
    """list[float] → APSelectionMLP に変換 (モデル受信時)"""
    data = struct.pack(f"<{len(floats)}f", *floats)
    return APSelectionMLP.from_f32_flat(data)


def _wait_for_edge(client: httpx.Client, retries: int = 60, interval: float = 5.0) -> None:
    """エッジサーバーが起動するまで待機"""
    for i in range(retries):
        try:
            r = client.get(f"{EDGE_URL}/", timeout=3.0)
            if r.status_code < 500:
                log.info("Edge server is ready")
                return
        except Exception:
            pass
        log.info(f"Waiting for edge server... ({i+1}/{retries})")
        time.sleep(interval)
    raise RuntimeError(f"Edge server {EDGE_URL} did not become ready after {retries} retries")


def _retry_request(
    fn,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> httpx.Response:
    """
    HTTP リクエスト関数 fn() をリトライ付きで実行する。
    - TransportError (接続失敗など) → リトライ
    - 5xx レスポンス → リトライ
    - それ以外 → そのまま返す / 再送出
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = fn()
            if resp.status_code >= 500 and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                log.warning(f"Server error {resp.status_code}, retrying in {delay}s... ({attempt+1}/{max_retries})")
                time.sleep(delay)
                continue
            return resp
        except httpx.TransportError as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                log.warning(f"Transport error: {e}, retrying in {delay}s... ({attempt+1}/{max_retries})")
                time.sleep(delay)
            else:
                raise
    # ここには到達しないはずだが念のため
    raise last_exc  # type: ignore[misc]


def _try_download_model(client: httpx.Client) -> APSelectionMLP | None:
    """
    エッジサーバーから最新グローバルモデルをダウンロードする。
    モデルが準備できていない場合は None を返す。

    フロー:
      1. GET /send_to_device → links.model (URL) を取得
      2. URL から .bin (f32_flat) をダウンロード
      3. APSelectionMLP.from_f32_flat() でモデルを復元
    """
    try:
        r = _retry_request(lambda: client.get(f"{EDGE_URL}/send_to_device", timeout=10.0))
        if r.status_code != 200:
            return None
        pkg = r.json()

        model_url = pkg.get("links", {}).get("model")
        if not model_url:
            return None

        r2 = _retry_request(lambda: client.get(model_url, timeout=30.0))
        if r2.status_code != 200:
            return None

        data = r2.content
        if len(data) == 0 or len(data) % 4 != 0:
            log.debug(f"Invalid model binary: {len(data)} bytes")
            return None

        model = APSelectionMLP.from_f32_flat(data)
        log.info(f"Downloaded global model ({len(data)} bytes, {len(data)//4} params)")
        return model

    except Exception as e:
        log.debug(f"Model download skipped: {e}")
        return None


def _upload_weights(client: httpx.Client, model: APSelectionMLP) -> bool:
    """
    学習済み重みをエッジサーバーにアップロードする。
    実機 Android の HTTP POST /upload_biases に相当。
    """
    try:
        floats  = _model_to_floats(model)
        payload = {"biases": floats, "terminal_id": TERMINAL_ID}
        r = _retry_request(
            lambda: client.post(f"{EDGE_URL}/upload_biases", json=payload, timeout=30.0)
        )
        r.raise_for_status()
        log.info(f"Uploaded weights: {len(floats)} floats → edge OK")
        return True
    except Exception as e:
        log.warning(f"Upload failed: {e}")
        return False


def _get_congestion(client: httpx.Client, ap: str = "A") -> tuple[float, float]:
    """
    エッジサーバーの /api/v1/virtual_congestion/{id} から TP / RTT を取得する。
    実機の virtual_congestion.py エンドポイントと通信。

    Returns:
        (tp_mbps, rtt_ms)
    """
    try:
        r = _retry_request(
            lambda: client.get(
                f"{EDGE_URL}/api/v1/virtual_congestion/{TERMINAL_ID}",
                params  = {"ap": ap},
                timeout = 10.0,
            )
        )
        if r.status_code == 200:
            body = r.json()
            tp   = float(body.get("tp_actual_mbps", 20.0))
            rtt  = float(body.get("rtt_report_ms",  100.0))
            return tp, rtt
    except Exception as e:
        log.debug(f"Congestion request failed (ap={ap}): {e}")

    # フォールバック値 (AP_B デフォルト相当)
    return 20.0, 100.0


def _log_round(g_rnd: int, l_rnd: int | None, result: TrainingResult) -> None:
    """ラウンド結果をログ出力"""
    prefix = f"[G={g_rnd+1:02d}]"
    if l_rnd is not None:
        prefix += f"[L={l_rnd+1}]"
    log.info(
        f"{prefix} "
        f"train_acc={result.train_acc:.3f}  val_acc={result.val_acc:.3f}  "
        f"loss={result.train_loss:.4f}  dur={result.train_duration_ms:.0f}ms"
    )


# ── メインループ ──────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info(f"HFL Virtual Terminal Client")
    log.info(f"  terminal_id  : {TERMINAL_ID}")
    log.info(f"  edge_url     : {EDGE_URL}")
    log.info(f"  app_category : {APP_CATEGORY} (idx={app_idx})")
    log.info(f"  num_rounds   : {NUM_ROUNDS}")
    log.info(f"  local_rounds : {LOCAL_ROUNDS} × {EPOCHS} epochs")
    log.info(f"  seed         : {SEED}")
    log.info("=" * 60)

    torch.manual_seed(SEED)
    random.seed(SEED)

    # ── 合成データ生成 (sim/data_gen.py と同一ロジック) ──
    log.info(f"Generating synthetic dataset: {NUM_SAMPLES} samples...")
    dataset = APSelectionDataset(n_samples=NUM_SAMPLES, seed=SEED)
    log.info("Dataset ready")

    # ── SimTerminal 初期化 ──
    terminal = SimTerminal(
        terminal_id   = TERMINAL_ID,
        local_data    = dataset,
        input_size    = INPUT_SIZE,
        hidden_size   = HIDDEN_SIZE,
        output_size   = OUTPUT_SIZE,
        dropout_p     = 0.1,
        lr            = LR,
        weight_decay  = WEIGHT_DECAY,
        clip_max_norm = CLIP_NORM,
        warmup_steps  = WARMUP,
        min_lr_ratio  = MIN_LR_RATIO,
        batch_size    = BATCH_SIZE,
        seed          = SEED,
    )

    with httpx.Client() as client:
        # エッジサーバー起動待機
        _wait_for_edge(client)

        run_id  = f"docker-run-{TERMINAL_ID}"
        results = []

        for g_rnd in range(NUM_ROUNDS):
            log.info(f"━━━ Global Round {g_rnd+1:02d}/{NUM_ROUNDS} ━━━")

            # ── ① グローバルモデルのダウンロード試行 ──
            dl_model = _try_download_model(client)
            if dl_model is not None:
                terminal.set_global_weights(dl_model)
                log.info("Global model loaded from edge")
            else:
                log.info("No global model available yet, using current weights")

            # ── ② ローカルラウンド × LOCAL_ROUNDS ──
            for l_rnd in range(LOCAL_ROUNDS):
                round_id = g_rnd * LOCAL_ROUNDS + l_rnd

                result: TrainingResult = terminal.local_train(
                    epochs   = EPOCHS,
                    run_id   = run_id,
                    round_id = round_id,
                )
                _log_round(g_rnd, l_rnd, result)

                # 各ローカルラウンド後に重みをアップロード (実機と同一タイミング)
                _upload_weights(client, terminal.model)

            # ── ③ TP / RTT 取得 → 推論 → AP 選択 ──
            tp_a, rtt_a = _get_congestion(client, "A")
            tp_b, rtt_b = _get_congestion(client, "B")

            log.info(
                f"  Network: AP_A tp={tp_a:.1f}Mbps rtt={rtt_a:.0f}ms | "
                f"AP_B tp={tp_b:.1f}Mbps rtt={rtt_b:.0f}ms"
            )

            # オラクル最適 AP (TerminalSatisfaction 式で比較)
            sat_a   = terminal_satisfaction(app_idx, tp_a, rtt_a)
            sat_b   = terminal_satisfaction(app_idx, tp_b, rtt_b)
            best_ap = 0 if sat_a >= sat_b else 1

            inf_result = terminal.run_inference(
                run_id   = run_id,
                round_id = g_rnd,
                ap_tp_a  = tp_a,  ap_rtt_a = rtt_a,
                ap_tp_b  = tp_b,  ap_rtt_b = rtt_b,
                app_idx  = app_idx,
                best_ap  = best_ap,
            )

            log.info(
                f"  Inference: AP={'A' if inf_result.predicted_ap==0 else 'B'} "
                f"(switched={inf_result.switched}) "
                f"satisfaction={inf_result.satisfaction:.3f} "
                f"tier={inf_result.predicted_tier} "
                f"conf={inf_result.ap_confidence:.3f}"
            )

            results.append({
                "global_round"        : g_rnd + 1,
                "val_acc"             : round(result.val_acc,           4),
                "train_acc"           : round(result.train_acc,         4),
                "satisfaction"        : round(inf_result.satisfaction,  4),
                "satisfaction_before" : round(inf_result.satisfaction_before, 4),
                "switched"            : inf_result.switched,
                "predicted_ap"        : "A" if inf_result.predicted_ap == 0 else "B",
            })

            # ラウンド間の短いインターバル
            time.sleep(2.0)

        # ── 最終サマリー ──
        log.info("=" * 60)
        log.info(f"Training complete: {NUM_ROUNDS} global rounds")
        avg_acc  = sum(r["val_acc"]       for r in results) / len(results)
        avg_sat  = sum(r["satisfaction"]  for r in results) / len(results)
        n_switch = sum(1 for r in results if r["switched"])
        log.info(f"  avg val_acc    : {avg_acc:.3f}")
        log.info(f"  avg satisfaction: {avg_sat:.3f}")
        log.info(f"  AP switches    : {n_switch}/{NUM_ROUNDS}")
        log.info("=" * 60)

        # 結果を JSON で保存
        out_path = Path("/app/logs") / f"{TERMINAL_ID}_results.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        log.info(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()

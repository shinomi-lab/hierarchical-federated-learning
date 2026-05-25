#!/usr/bin/env python3
from __future__ import annotations
"""
scripts/sim_terminal.py
=======================
仮想 Android 端末シミュレータ。
エッジサーバの /receive_terminal_weights/{terminal_id} に
ダミーの学習結果（重み + メトリクス）を送信する。

CLI 使用例:
  python scripts/sim_terminal.py --url http://127.0.0.1:8001 --id term-01 --rounds 3
  python scripts/sim_terminal.py --url http://127.0.0.1:8001 --id term-01 --rounds 5 \
    --app-type video --n-samples 120

ライブラリ使用例 (test_runner.py から呼ぶ):
  from scripts.sim_terminal import run_terminal
  result = run_terminal(edge_url="http://127.0.0.1:8001", terminal_id="term-01", rounds=3)
"""

import argparse
import io
import random
import struct
import time
import uuid
from typing import Optional

# httpx は任意、なければ requests にフォールバック
try:
    import httpx as _http_lib
    _USE_HTTPX = True
except ImportError:
    import requests as _http_lib  # type: ignore
    _USE_HTTPX = False


# ------------------------------------------------------------------ #
# ダミー重みの生成
# ------------------------------------------------------------------ #
INPUT_SIZE  = 4    # 4 features (RSSI, RTT, bandwidth, congestion)
HIDDEN_SIZE = 8
OUTPUT_SIZE = 2    # 2 AP candidates

def _make_dummy_weights(seed: int = 0) -> bytes:
    """
    端末のローカル学習後の重みを模倣した f32_flat バイト列を返す。
    (LayerNorm なし、2層 MLP: layer1.weight, layer1.bias, layer3.weight, layer3.bias)
    """
    rng = random.Random(seed)
    tensors = [
        [rng.gauss(0, 0.1) for _ in range(HIDDEN_SIZE * INPUT_SIZE)],   # layer1.weight
        [rng.gauss(0, 0.05) for _ in range(HIDDEN_SIZE)],               # layer1.bias
        [rng.gauss(0, 0.1) for _ in range(OUTPUT_SIZE * HIDDEN_SIZE)],  # layer3.weight
        [rng.gauss(0, 0.05) for _ in range(OUTPUT_SIZE)],               # layer3.bias
    ]
    vals = [v for t in tensors for v in t]
    return struct.pack(f"<{len(vals)}f", *vals)


# ------------------------------------------------------------------ #
# 1ラウンド分の送信
# ------------------------------------------------------------------ #
APP_TYPES = ["browser", "video", "call", "other"]

def _send_round(
    session,
    edge_url: str,
    terminal_id: str,
    round_id: int,
    n_samples: int,
    app_type: str,
    seed: int,
    verbose: bool = True,
) -> dict:
    """
    1 ラウンド分の重み＋メトリクスをエッジサーバに送信する。
    戻り値: {"ok": bool, "status": int, "body": dict|str}
    """
    weights_bytes = _make_dummy_weights(seed)
    satisfaction_before = round(random.uniform(0.3, 0.8), 3)
    satisfaction_after  = round(satisfaction_before + random.uniform(-0.1, 0.3), 3)
    satisfaction_after  = max(0.0, min(1.0, satisfaction_after))
    accuracy = round(random.uniform(0.5, 0.95), 4)
    loss     = round(random.uniform(0.05, 0.5), 4)

    url = f"{edge_url.rstrip('/')}/receive_terminal_weights/{terminal_id}"
    n_total = len(_make_dummy_weights()) // 4  # float32 count
    data = {
        "round_id":           str(round_id),
        "model_id":           "sim-model",
        "base_hash":          f"basehash-r{round_id}",
        "n_samples":          str(n_samples),
        "payload_kind":       "full",
        "dtype":              "f32_flat",
        "input_size":         str(INPUT_SIZE),
        "hidden_size":        str(HIDDEN_SIZE),
        "output_size":        str(OUTPUT_SIZE),
        "reqId":              str(uuid.uuid4()),
        "accuracy":           str(accuracy),
        "loss":               str(loss),
        "app_type":           app_type,
        "app_index":          str(APP_TYPES.index(app_type) if app_type in APP_TYPES else 0),
        "satisfaction_before": str(satisfaction_before),
        "satisfaction_after":  str(satisfaction_after),
    }

    try:
        if _USE_HTTPX:
            resp = session.post(
                url,
                data=data,
                files={"weights": ("weights.bin", io.BytesIO(weights_bytes), "application/octet-stream")},
                timeout=30.0,
            )
            status = resp.status_code
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:200]
        else:
            resp = session.post(
                url,
                data=data,
                files={"weights": ("weights.bin", io.BytesIO(weights_bytes), "application/octet-stream")},
                timeout=30.0,
            )
            status = resp.status_code
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:200]

        ok = (status == 200)
        if verbose:
            mark = "✓" if ok else "✗"
            print(
                f"  [{mark}] {terminal_id} round={round_id}  "
                f"acc={accuracy:.4f} sat {satisfaction_before:.3f}→{satisfaction_after:.3f}  "
                f"app={app_type}  HTTP {status}"
            )
        return {"ok": ok, "status": status, "body": body}

    except Exception as exc:
        if verbose:
            print(f"  [!] {terminal_id} round={round_id}  エラー: {exc}")
        return {"ok": False, "status": 0, "body": str(exc)}


# ------------------------------------------------------------------ #
# 複数ラウンド実行
# ------------------------------------------------------------------ #
def run_terminal(
    edge_url: str,
    terminal_id: str,
    rounds: int = 3,
    n_samples: int = 100,
    app_type: Optional[str] = None,
    interval: float = 0.5,
    verbose: bool = True,
) -> list[dict]:
    """
    指定ラウンド数だけ重みを送信する。
    戻り値: 各ラウンドの結果リスト
    """
    if app_type is None:
        app_type = random.choice(APP_TYPES)

    results = []
    if _USE_HTTPX:
        ctx = _http_lib.Client(timeout=30.0)
    else:
        ctx = _http_lib.Session()

    with ctx as session:
        for r in range(1, rounds + 1):
            seed = hash(terminal_id) ^ r
            res = _send_round(
                session, edge_url, terminal_id, r,
                n_samples=n_samples, app_type=app_type,
                seed=seed, verbose=verbose,
            )
            results.append(res)
            if r < rounds:
                time.sleep(interval)

    return results


# ------------------------------------------------------------------ #
# CLI エントリポイント
# ------------------------------------------------------------------ #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="仮想 Android 端末シミュレータ",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--url",       default="http://127.0.0.1:8001", help="エッジサーバの URL")
    p.add_argument("--id",        default="sim-term-01",           help="端末 ID")
    p.add_argument("--rounds",    type=int, default=3,             help="送信するラウンド数")
    p.add_argument("--n-samples", type=int, default=100,           help="学習サンプル数")
    p.add_argument("--app-type",  default=None,                    help="アプリ種別 (browser/video/call/other)")
    p.add_argument("--interval",  type=float, default=0.5,         help="ラウンド間の待機秒数")
    p.add_argument("--quiet",     action="store_true",             help="詳細ログを抑制")
    return p


def main():
    args = _build_parser().parse_args()
    print(f"[sim_terminal] {args.id}  {args.rounds} ラウンド → {args.url}")
    results = run_terminal(
        edge_url=args.url,
        terminal_id=args.id,
        rounds=args.rounds,
        n_samples=args.n_samples,
        app_type=args.app_type,
        interval=args.interval,
        verbose=not args.quiet,
    )
    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n結果: {ok_count}/{len(results)} ラウンド成功")
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

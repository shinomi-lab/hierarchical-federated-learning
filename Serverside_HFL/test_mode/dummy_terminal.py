#!/usr/bin/env python3
"""
dummy_terminal.py
=================
プロトコル忠実な PC ローカル端末エミュレータ。

役割:
  実機 Android クライアント (TrainingViewModel.runFiveCycles) と同じ
  HTTP プロトコル / state machine で動作するが、学習はせず固定形状の
  ランダム state_dict を送る。サーバ Python コードの統合検証のみが目的。

実機との対応:
  - fetchMeta()         → GET /api/v1/meta
  - performLocalTraining → ローカルでランダム state_dict 生成（学習スキップ）
  - upload              → POST /receive_terminal_weights/{terminal_id}
  - waitForServerRoundToAdvance → 同期: fetchMeta ポーリング
                                  非同期: withTimeout(1秒) で諦め

使い方:
  python3 dummy_terminal.py \
      --terminal-id terminal-01 \
      --edge-url http://127.0.0.1:8001 \
      --hfl-mode async \
      --cycles 5 \
      --rounds 3
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import random
import time
import uuid
from typing import Optional

import httpx
import torch


logger = logging.getLogger(__name__)


# =====================================================================
# TP/RTT 確率モデル (Sim_HFL/sim/network_model.py のミニ版を移植)
# =====================================================================
def _erlang_b(n_channels: int, offered: float) -> float:
    """Erlang-B blocking probability. Server virtual_congestion.py と同式。"""
    if offered <= 0:
        return 0.0
    p = 1.0
    for k in range(1, max(1, int(n_channels)) + 1):
        p = (offered * p) / (k + offered * p)
    return p


def _mm1_rtt(n_connected: int, mu: float, base_rtt_ms: float) -> float:
    """M/M/1 RTT。サーバ式: rtt = base / (1 - λ/μ) (飽和時は 9997)"""
    if mu <= 0 or n_connected >= mu:
        return 9997.0
    return base_rtt_ms / max(1.0 - n_connected / mu, 1e-9)


# 実機 ROUTER_PARAMS と整合 (virtual_congestion.py)
_AP_PROFILES = {
    "HFL_A24": dict(tp_init=50.0, rtt_init=20.0, mu=2.0, n_channels=2,
                    rtt_noise=2.0, tp_noise=1.0),     # 自宅 WiFi 相当
    "HFL_B":   dict(tp_init=20.0, rtt_init=80.0, mu=5.0, n_channels=10,
                    rtt_noise=5.0, tp_noise=1.0),     # Cellular 相当
}


def synth_tp_rtt(
    mode: str,
    n_connected_on_ap: int = 1,
    ap_profile: str = "HFL_A24",
    rng: "random.Random | None" = None,
) -> tuple[float, float]:
    """metric-mode に応じて (tp_mbps, rtt_ms) を生成する。

    Modes:
      fixed       : 実機固定値 (tp=50, rtt=20)。互換用
      noisy       : N(50, 5) / N(20, 5) のガウスノイズ
      network     : Erlang-B + M/M/1 で実機 ROUTER_PARAMS と一致した混雑モデル
    """
    if rng is None:
        rng = random
    if mode == "fixed":
        return 50.0, 20.0
    if mode == "noisy":
        tp = max(0.5, rng.gauss(50.0, 5.0))
        rtt = max(1.0, rng.gauss(20.0, 5.0))
        return round(tp, 2), round(rtt, 2)
    # mode == "network"
    p = _AP_PROFILES.get(ap_profile, _AP_PROFILES["HFL_A24"])
    rtt_base = _mm1_rtt(n_connected_on_ap, p["mu"], p["rtt_init"])
    rtt = max(1.0, rtt_base + rng.gauss(0.0, p["rtt_noise"]))
    offered = n_connected_on_ap / max(p["mu"], 1e-9)
    p_loss = _erlang_b(p["n_channels"], offered)
    tp_base = p["tp_init"] * (1.0 - p_loss)
    tp = max(0.1, tp_base + rng.gauss(0.0, p["tp_noise"]))
    return round(tp, 2), round(rtt, 2)


# 6→32→32→4 MLP (実機 global_initial_mobile と同形状)
MODEL_SHAPE = {
    "layer1.weight": (32, 6),
    "layer1.bias":   (32,),
    "norm1.weight":  (32,),
    "norm1.bias":    (32,),
    "layer2.weight": (32, 32),
    "layer2.bias":   (32,),
    "norm2.weight":  (32,),
    "norm2.bias":    (32,),
    "layer3.weight": (4, 32),
    "layer3.bias":   (4,),
}


def make_random_state_dict() -> dict:
    """ランダム重みの state_dict を作る。実機 LocalTrainer の結果と同形状。"""
    sd = {}
    for k, shape in MODEL_SHAPE.items():
        if "norm" in k and "weight" in k:
            sd[k] = torch.ones(shape) + torch.randn(shape) * 0.01
        elif "bias" in k:
            sd[k] = torch.zeros(shape) + torch.randn(shape) * 0.01
        else:
            sd[k] = torch.randn(shape) * 0.1
    return sd


def state_dict_to_bytes(sd: dict) -> bytes:
    """torch_state_dict 形式でシリアライズ。"""
    buf = io.BytesIO()
    torch.save(sd, buf)
    return buf.getvalue()


class DummyTerminal:
    """実機 TrainingViewModel.runFiveCycles と同等の HTTP state machine."""

    def __init__(
        self,
        terminal_id: str,
        edge_url: str,
        hfl_mode: str = "sync",
        n_samples: int = 1000,
        async_timeout_ms: int = 1000,
        metric_mode: str = "noisy",
        n_terminals_per_ap: int = 2,
        ap_profile: str = "HFL_A24",
        seed: int = 0,
    ):
        self.terminal_id = terminal_id
        self.edge_url = edge_url.rstrip("/")
        self.hfl_mode = hfl_mode
        self.n_samples = n_samples
        self.async_timeout_s = async_timeout_ms / 1000
        self.run_id = str(uuid.uuid4())
        self.local_seq = 0
        # 直近 fetchMeta で取得したサーバラウンド
        self.current_server_round: int = 0
        self.current_model_id: str = "demo-mlp-v1"
        self.current_base_hash: str = "sha256:bootstrap"
        # TP/RTT 確率モデル設定
        self.metric_mode = metric_mode
        self.n_terminals_per_ap = max(1, int(n_terminals_per_ap))
        self.ap_profile = ap_profile
        self._metric_rng = random.Random(seed if seed else hash(terminal_id) & 0xFFFFFFFF)

    async def fetch_meta(self, client: httpx.AsyncClient) -> dict:
        """GET /api/v1/meta — 実機 NetworkClient.fetchMeta と同等"""
        r = await client.get(f"{self.edge_url}/api/v1/meta", timeout=10.0)
        r.raise_for_status()
        meta = r.json()
        self.current_server_round = int(meta.get("round", 0))
        self.current_model_id = str(meta.get("model_id", self.current_model_id))
        self.current_base_hash = str(meta.get("base_hash", self.current_base_hash))
        return meta

    async def upload_weights(
        self,
        client: httpx.AsyncClient,
        round_id: int,
        sd: dict,
    ) -> tuple[int, dict]:
        """POST /receive_terminal_weights/{terminal_id} — multipart"""
        self.local_seq += 1
        wb = state_dict_to_bytes(sd)
        sha = "sha256:" + hashlib.sha256(wb).hexdigest()

        # 実機なら ConnectivityManager / Socket から取る TP/RTT を、
        # metric_mode に応じて確率的に生成する (new_policy.md TEST mode 要件)
        _tp, _rtt = synth_tp_rtt(
            mode               = self.metric_mode,
            n_connected_on_ap  = self.n_terminals_per_ap,
            ap_profile         = self.ap_profile,
            rng                = self._metric_rng,
        )

        client_meta = json.dumps({
            "terminal_id": self.terminal_id,
            "total_local_ms": random.randint(5000, 12000),
            "epoch_durations_ms": [random.randint(1000, 3000) for _ in range(5)],
            "timestamp_ms": int(time.time() * 1000),
            "virtual_router_id": "HFL_TEST",
            "session_id": self.run_id,
            "run_id": self.run_id,
            "experiment_group": "test_mode",
            "app_index": int(self.terminal_id.split("-")[-1]) % 4 if "-" in self.terminal_id else 0,
            "app_type": "test",
            "cycle_number": self.local_seq,
            "network_type_at_upload": "WIFI",
            "battery_at_start": 80,
            "battery_at_end": 80,
            "model_version": "",
            "tp_measured_mbps": _tp,
            "rtt_measured_ms": _rtt,
            "accuracy": 0.8 + random.random() * 0.1,
            "loss": 0.5 + random.random() * 0.2,
        })

        data = {
            "round_id":      str(round_id),
            "model_id":      self.current_model_id,
            "base_hash":     self.current_base_hash,
            "n_samples":     str(self.n_samples),
            "payload_kind":  "full",
            "dtype":         "torch_state_dict",
            "force_aggregate": "false",
            "client_meta":   client_meta,
            "input_size":    "6",
            "hidden_size":   "32",
            "output_size":   "4",
            "contentSha256": sha,
            "reqId":         str(uuid.uuid4()),
            "virtual_router_id": "HFL_TEST",
            "run_id":        self.run_id,
            "local_seq":     str(self.local_seq),
        }
        # dtype=torch_state_dict には .pt / .pth が要求される (terminal_update._is_valid_ext)
        files = {
            "weights": ("weights.pt", wb, "application/octet-stream"),
        }
        url = f"{self.edge_url}/receive_terminal_weights/{self.terminal_id}"
        r = await client.post(url, data=data, files=files, timeout=30.0)
        try:
            body = r.json() if r.content else {}
        except Exception:
            body = {"_raw": r.text[:200]}
        return r.status_code, body

    async def wait_for_round_to_advance(
        self,
        client: httpx.AsyncClient,
        prev_round: int,
        max_wait_s: float,
    ) -> bool:
        """サーバが次ラウンドに進むまでポーリング (sync) / 非同期は短いタイムアウト"""
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            try:
                await self.fetch_meta(client)
                if self.current_server_round > prev_round:
                    return True
            except Exception as e:
                logger.warning(f"[{self.terminal_id}] fetch_meta failed: {e}")
            await asyncio.sleep(0.5)
        return False

    async def run_cycle(self, client: httpx.AsyncClient, cycle_idx: int) -> dict:
        """1 ローカルサイクル = 学習 (スキップ) → upload → wait"""
        # ① 学習開始前にメタ取得 (round_id を同期)
        await self.fetch_meta(client)
        prev_round = self.current_server_round

        # ② ローカル学習スキップ → ランダム state_dict
        sd = make_random_state_dict()
        await asyncio.sleep(random.uniform(0.1, 0.5))   # 学習時間の擬似

        # ③ アップロード
        status, body = await self.upload_weights(client, prev_round, sd)
        logger.info(
            f"[{self.terminal_id}] cycle={cycle_idx} upload status={status} "
            f"round={prev_round} body_keys={list(body.keys())[:3]}"
        )

        # ④ 次ラウンド進行を待つ
        if self.hfl_mode == "async":
            advanced = await self.wait_for_round_to_advance(client, prev_round, self.async_timeout_s)
        else:
            advanced = await self.wait_for_round_to_advance(client, prev_round, 60.0)

        return {
            "cycle": cycle_idx,
            "round_used": prev_round,
            "upload_status": status,
            "round_after": self.current_server_round,
            "advanced": advanced,
        }

    async def run(self, cycles: int = 5, rounds: int = 1):
        """rounds × cycles 回サイクルを回す"""
        async with httpx.AsyncClient() as client:
            await self.fetch_meta(client)
            logger.info(
                f"[{self.terminal_id}] start "
                f"hfl_mode={self.hfl_mode} initial_round={self.current_server_round}"
            )
            results = []
            for g in range(rounds):
                for c in range(1, cycles + 1):
                    res = await self.run_cycle(client, cycle_idx=c)
                    results.append(res)
            return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--terminal-id", required=True)
    p.add_argument("--edge-url", default="http://127.0.0.1:8001")
    p.add_argument("--hfl-mode", default="sync", choices=["sync", "async"])
    p.add_argument("--cycles", type=int, default=5)
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--n-samples", type=int, default=1000)
    p.add_argument("--async-timeout-ms", type=int, default=1000)
    p.add_argument("--start-delay-s", type=float, default=0.0,
                   help="起動直後の遅延 (端末間の起動タイミング分散用)")
    # --- TP/RTT スタブ (new_policy.md TEST mode 要件) ---
    p.add_argument(
        "--metric-mode", default="noisy",
        choices=["fixed", "noisy", "network"],
        help="TP/RTT 生成モード。"
             "fixed=実機固定値, noisy=N(50,5)/N(20,5) ガウスノイズ, "
             "network=Erlang-B + M/M/1 (ROUTER_PARAMS 整合)"
    )
    p.add_argument("--ap-profile", default="HFL_A24",
                   choices=["HFL_A24", "HFL_B"],
                   help="network モード時の AP 設定。WiFi=HFL_A24, Cellular=HFL_B")
    p.add_argument("--n-on-ap", type=int, default=2,
                   help="network モード時、自分が接続中の AP に何台繋がっているか")
    p.add_argument("--metric-seed", type=int, default=0,
                   help="TP/RTT 乱数シード (0 なら terminal_id ハッシュ)")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.start_delay_s > 0:
        time.sleep(args.start_delay_s)

    term = DummyTerminal(
        terminal_id        = args.terminal_id,
        edge_url           = args.edge_url,
        hfl_mode           = args.hfl_mode,
        n_samples          = args.n_samples,
        async_timeout_ms   = args.async_timeout_ms,
        metric_mode        = args.metric_mode,
        n_terminals_per_ap = args.n_on_ap,
        ap_profile         = args.ap_profile,
        seed               = args.metric_seed,
    )
    results = asyncio.run(term.run(cycles=args.cycles, rounds=args.rounds))
    print(json.dumps({"terminal_id": args.terminal_id, "results": results}, indent=2))


if __name__ == "__main__":
    main()

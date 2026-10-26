#!/usr/bin/env python3
"""中央モデル（centralized-mode の対照群）を、教師 CSV の全行でオフライン学習する。
設定は端末 LocalTrainer / SimpleDataLoader.loadFlexible に合わせる（openspec/changes/centralized-mode/design.md D2）。

実行: Serverside_HFL/.venv/bin/python3 Serverside_HFL/tools/train_central_model.py training_data_list/default/training_data.csv \
        --out-dir Serverside_HFL/state --epochs 25 --seed 42
出力: <out-dir>/central_model.pt（state_dict）と central_model.meta.json（行数・epochs・seed・精度・SHA256 ほか）
"""
import argparse
import hashlib
import json
import math
import time
from collections import OrderedDict
from pathlib import Path

import sys

import torch
import torch.nn as nn

# スクリプトとして直接実行したとき（sys.path[0] が tools/ になる）でも shared/ を import できるように
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.central_model import state_sha256  # noqa: E402

INPUT_SIZE, HIDDEN_SIZE, OUTPUT_SIZE, NUM_APPS = 6, 32, 2, 4
DROPOUT, LR, WEIGHT_DECAY, BATCH, CLIP = 0.1, 1e-4, 0.01, 16, 1.0
WARMUP_STEPS, MIN_LR_RATIO = 10, 0.1


def load_dataset(csv_path):
    """CSV（tpNeed, rttNeed, appNum, label。ヘッダ無し）→ (特徴量 N×6, ラベル N, 正規化統計)。"""
    raw = []
    for line in Path(csv_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        c = [s.strip() for s in line.split(",")]
        raw.append((float(c[0]), float(c[1]), int(c[2]), int(c[3])))
    if not raw:
        raise ValueError(f"教師データが空です: {csv_path}")
    stats = {}
    cols = {"tp": [r[0] for r in raw], "rtt": [r[1] for r in raw]}
    for k, v in cols.items():
        m = sum(v) / len(v)
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
        stats[f"{k}_mean"], stats[f"{k}_std"] = m, (sd if sd > 1e-8 else 1.0)
    x = torch.zeros(len(raw), INPUT_SIZE, dtype=torch.float32)
    for i, (tp, rtt, app, _) in enumerate(raw):
        x[i, 0] = (tp - stats["tp_mean"]) / (stats["tp_std"] + 1e-8)
        x[i, 1] = (rtt - stats["rtt_mean"]) / (stats["rtt_std"] + 1e-8)
        if not 0 <= app < NUM_APPS:
            raise ValueError(f"appNum が範囲外です（0〜{NUM_APPS - 1}）: {app}")
        x[i, 2 + app] = 1.0
    y = torch.tensor([r[3] for r in raw], dtype=torch.long)
    return x, y, stats


def _build_model():
    """端末 MLPModel と同じ構造。Linear の重みは U(-1/√in, 1/√in)（torch 既定と同じ）、バイアスは 0。"""
    l1, l2, l3 = nn.Linear(INPUT_SIZE, HIDDEN_SIZE), nn.Linear(HIDDEN_SIZE, HIDDEN_SIZE), nn.Linear(HIDDEN_SIZE, OUTPUT_SIZE)
    for lin in (l1, l2, l3):
        nn.init.zeros_(lin.bias)
    n1, n2 = nn.LayerNorm(HIDDEN_SIZE), nn.LayerNorm(HIDDEN_SIZE)
    return nn.ModuleDict({
        "layer1": l1, "norm1": n1, "layer2": l2, "norm2": n2, "layer3": l3,
    })


def _forward(m, x, train):
    h = torch.relu(m["norm1"](m["layer1"](x)))
    h = nn.functional.dropout(h, DROPOUT, training=train)
    h = torch.relu(m["norm2"](m["layer2"](h)))
    h = nn.functional.dropout(h, DROPOUT, training=train)
    return m["layer3"](h)


def _lr_factor(step, total_steps):
    """端末 LinearWarmupScheduler と同じ。step は学習済みステップ数（最初のステップは基準 lr のまま）。"""
    if step == 0:
        return 1.0
    if step < WARMUP_STEPS:
        return step / WARMUP_STEPS
    if step > total_steps:
        return MIN_LR_RATIO
    decay = (step - WARMUP_STEPS) / max(1, total_steps - WARMUP_STEPS)
    return (1 - MIN_LR_RATIO) * (1 - decay) + MIN_LR_RATIO


def _evaluate(m, x, y):
    if len(y) == 0:
        return 0.0, 0.0
    with torch.no_grad():
        logits = _forward(m, x, train=False)
        loss = nn.functional.cross_entropy(logits, y).item()
        acc = (logits.argmax(dim=1) == y).float().mean().item()
    return loss, acc


def train(csv_path, out_dir, epochs=25, seed=42):
    started = time.perf_counter()
    x, y, norm = load_dataset(csv_path)
    n = len(y)
    n_train, n_val = int(n * 0.8), int(n * 0.1)
    if n >= 3:
        n_train, n_val = max(n_train, 1), max(n_val, 1)
        if n - n_train - n_val < 1:
            n_train = n - n_val - 1
    # 端末と同じくファイル順で 学習/検証/テスト に分ける
    xt, yt = x[:n_train], y[:n_train]
    xv, yv = x[n_train:n_train + n_val], y[n_train:n_train + n_val]
    xs, ys = x[n_train + n_val:], y[n_train + n_val:]

    torch.manual_seed(seed)
    model = _build_model()
    decay = [p for k, mod in model.items() if k.startswith("layer") for p in mod.parameters()]
    no_decay = [p for k, mod in model.items() if k.startswith("norm") for p in mod.parameters()]
    opt = torch.optim.AdamW([{"params": decay, "weight_decay": WEIGHT_DECAY}, {"params": no_decay, "weight_decay": 0.0}], lr=LR)
    total_steps = (n // BATCH) * epochs if n else epochs
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: _lr_factor(s, total_steps))
    gen = torch.Generator().manual_seed(seed)
    params = [p for g in opt.param_groups for p in g["params"]]

    val_loss = val_acc = 0.0
    for _ in range(epochs):
        perm = torch.randperm(len(yt), generator=gen)
        for i in range(0, len(yt), BATCH):
            idx = perm[i:i + BATCH]
            opt.zero_grad()
            loss = nn.functional.cross_entropy(_forward(model, xt[idx], train=True), yt[idx])
            loss.backward()
            torch.nn.utils.clip_grad_norm_(params, CLIP)
            opt.step()
            sched.step()
        val_loss, val_acc = _evaluate(model, xv, yv)  # HFL のメトリクスと同じく「最終エポックの値」を残す
    _, test_acc = _evaluate(model, xs, ys)

    sd = OrderedDict()
    for name in ("layer1", "norm1", "layer2", "norm2", "layer3"):
        sd[f"{name}.weight"] = model[name].weight.detach().clone()
        sd[f"{name}.bias"] = model[name].bias.detach().clone()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    torch.save(sd, out / "central_model.pt")
    meta = {
        "csv": str(csv_path),
        "csv_sha256": hashlib.sha256(Path(csv_path).read_bytes()).hexdigest(),
        "rows": n, "train_rows": len(yt), "val_rows": len(yv), "test_rows": len(ys),
        "epochs": epochs, "seed": seed,
        "val_accuracy": val_acc, "val_loss": val_loss, "test_accuracy": test_acc,
        "model_sha256": state_sha256(sd),
        "train_seconds": time.perf_counter() - started,
        "norm": norm,
        "hyperparams": {"lr": LR, "weight_decay": WEIGHT_DECAY, "batch": BATCH, "dropout": DROPOUT,
                        "clip": CLIP, "warmup_steps": WARMUP_STEPS, "min_lr_ratio": MIN_LR_RATIO,
                        "total_steps": total_steps},
        "torch": torch.__version__,
    }
    (out / "central_model.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--out-dir", default="state")
    ap.add_argument("--epochs", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args(argv)
    meta = train(a.csv, a.out_dir, epochs=a.epochs, seed=a.seed)
    print(json.dumps({k: meta[k] for k in ("rows", "epochs", "seed", "val_accuracy", "test_accuracy", "model_sha256", "train_seconds")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

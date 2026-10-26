#!/usr/bin/env python3
"""中央の初期グローバルモデル（端末 LocalTrainer と同じ構造: Linear→LayerNorm→ReLU ×2 → Linear）を作る。
実行: Serverside_HFL/.venv/bin/python3 Serverside_HFL/tools/make_initial_model.py Serverside_HFL/state/global_model_mobile.pt
"""
import sys
from collections import OrderedDict

import torch
import torch.nn as nn


def build_state_dict(input_size=6, hidden_size=32, output_size=2, seed=42):
    torch.manual_seed(seed)
    l1, l2, l3 = nn.Linear(input_size, hidden_size), nn.Linear(hidden_size, hidden_size), nn.Linear(hidden_size, output_size)
    n1, n2 = nn.LayerNorm(hidden_size), nn.LayerNorm(hidden_size)
    sd = OrderedDict()
    for name, mod in (("layer1", l1), ("norm1", n1), ("layer2", l2), ("norm2", n2), ("layer3", l3)):
        sd[f"{name}.weight"] = mod.weight.detach().clone()
        sd[f"{name}.bias"] = mod.bias.detach().clone()
    return sd


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "global_model_mobile.pt"
    torch.save(build_state_dict(), out)
    print("wrote", out)

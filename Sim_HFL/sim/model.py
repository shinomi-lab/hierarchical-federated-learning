"""
sim/model.py
------------
実機 (LocalTrainer.kt) と完全同一アーキテクチャの AP 選択 MLP。

Architecture (LocalTrainer.kt の MLPModel に対応):
  layer1  : Linear(input_size → hidden_size)
  norm1   : LayerNorm(hidden_size)          ← γ/β あり
  relu1   : ReLU
  dropout1: Dropout(dropout_p)
  layer2  : Linear(hidden_size → hidden_size)
  norm2   : LayerNorm(hidden_size)          ← γ/β あり
  relu2   : ReLU
  dropout2: Dropout(dropout_p)
  layer3  : Linear(hidden_size → output_size)

f32_flat レイアウト (LocalTrainer.kt の loadFromFlat と完全一致):
  layer1.weight  [hidden × input]
  layer1.bias    [hidden]
  norm1.weight   [hidden]   (gamma)
  norm1.bias     [hidden]   (beta)
  layer2.weight  [hidden × hidden]
  layer2.bias    [hidden]
  norm2.weight   [hidden]   (gamma)
  norm2.bias     [hidden]   (beta)
  layer3.weight  [output × hidden]
  layer3.bias    [output]
"""
from __future__ import annotations

import struct
import torch
import torch.nn as nn
import torch.nn.functional as F


INPUT_SIZE  = 6   # [TP, RTT, app_one_hot(4D)]  (実機 tpRttFeatures=2 + APP_CAT_COUNT=4)
HIDDEN_SIZE = 32  # 実機 hiddenSize = 32
OUTPUT_SIZE = 4   # 実機 outputSize = APP_CAT_COUNT = 4


class APSelectionMLP(nn.Module):
    """
    AP 選択 3層 MLP。実機 LocalTrainer.kt の MLPModel と同一構造。
    """

    def __init__(
        self,
        input_size:  int   = INPUT_SIZE,
        hidden_size: int   = HIDDEN_SIZE,
        output_size: int   = OUTPUT_SIZE,
        dropout_p:   float = 0.0,
    ) -> None:
        super().__init__()
        self.layer1   = nn.Linear(input_size,  hidden_size)
        self.norm1    = nn.LayerNorm(hidden_size)
        self.dropout1 = nn.Dropout(dropout_p)
        self.layer2   = nn.Linear(hidden_size, hidden_size)
        self.norm2    = nn.LayerNorm(hidden_size)
        self.dropout2 = nn.Dropout(dropout_p)
        self.layer3   = nn.Linear(hidden_size, output_size)

        # [実機一致] Kotlin の LinearLayer: biases = MutableList(outputDim) { 0.0f }
        # PyTorch デフォルトは Uniform(-1/sqrt(fan_in), 1/sqrt(fan_in)) なので上書き
        for layer in [self.layer1, self.layer2, self.layer3]:
            nn.init.zeros_(layer.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.dropout1(F.relu(self.norm1(self.layer1(x))))
        x = self.dropout2(F.relu(self.norm2(self.layer2(x))))
        return self.layer3(x)

    # ------------------------------------------------------------------ #
    # f32_flat シリアライズ (LocalTrainer.kt の loadFromFlat と同一順序)
    # ------------------------------------------------------------------ #
    def to_f32_flat(self) -> bytes:
        """
        全パラメータを f32_flat バイト列（リトルエンディアン）に変換。
        LayerNorm の γ/β を含む順序は LocalTrainer.kt に完全一致。
        """
        tensors = [
            self.layer1.weight,   # [hidden × input]
            self.layer1.bias,     # [hidden]
            self.norm1.weight,    # [hidden]  gamma
            self.norm1.bias,      # [hidden]  beta
            self.layer2.weight,   # [hidden × hidden]
            self.layer2.bias,     # [hidden]
            self.norm2.weight,    # [hidden]  gamma
            self.norm2.bias,      # [hidden]  beta
            self.layer3.weight,   # [output × hidden]
            self.layer3.bias,     # [output]
        ]
        vals: list[float] = []
        for t in tensors:
            vals.extend(t.detach().cpu().flatten().tolist())
        return struct.pack(f"<{len(vals)}f", *vals)

    @classmethod
    def from_f32_flat(
        cls,
        data:        bytes,
        input_size:  int   = INPUT_SIZE,
        hidden_size: int   = HIDDEN_SIZE,
        output_size: int   = OUTPUT_SIZE,
        dropout_p:   float = 0.0,
    ) -> "APSelectionMLP":
        """f32_flat バイト列からモデルを復元（LocalTrainer.kt と同一順序）"""
        model = cls(input_size, hidden_size, output_size, dropout_p)
        vals  = list(struct.unpack(f"<{len(data) // 4}f", data))
        idx   = 0

        def load(param: nn.Parameter) -> None:
            nonlocal idx
            n = param.numel()
            with torch.no_grad():
                param.copy_(torch.tensor(vals[idx : idx + n]).reshape(param.shape))
            idx += n

        load(model.layer1.weight)
        load(model.layer1.bias)
        load(model.norm1.weight)
        load(model.norm1.bias)
        load(model.layer2.weight)
        load(model.layer2.bias)
        load(model.norm2.weight)
        load(model.norm2.bias)
        load(model.layer3.weight)
        load(model.layer3.bias)
        return model

    def weight_norm(self) -> float:
        """全パラメータの L2 ノルム（理論比較・ドリフト分析用）"""
        total = sum(p.detach().pow(2).sum().item() for p in self.parameters())
        return total ** 0.5

    @staticmethod
    def param_count(
        input_size:  int = INPUT_SIZE,
        hidden_size: int = HIDDEN_SIZE,
        output_size: int = OUTPUT_SIZE,
    ) -> int:
        """
        総パラメータ数を返す。
        layer1: input×hidden + hidden
        norm1:  hidden + hidden
        layer2: hidden×hidden + hidden
        norm2:  hidden + hidden
        layer3: hidden×output + output
        """
        return (
            input_size  * hidden_size + hidden_size +   # layer1
            hidden_size + hidden_size +                  # norm1 (gamma, beta)
            hidden_size * hidden_size + hidden_size +   # layer2
            hidden_size + hidden_size +                  # norm2 (gamma, beta)
            hidden_size * output_size + output_size      # layer3
        )

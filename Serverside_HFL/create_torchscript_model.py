import os
import torch
from torch import nn


model_params = [
    int(os.environ.get("INPUT_SIZE",  "6")),
    int(os.environ.get("HIDDEN_SIZE", "32")),
    int(os.environ.get("OUTPUT_SIZE", "3")),
]

def create_initial_model(model_params=model_params):
    """
    初期グローバルモデルを作成する。
    アーキテクチャは LocalTrainer.kt / Sim_HFL model.py と完全一致:
      layer1  → norm1 → ReLU → dropout1
      layer2  → norm2 → ReLU → dropout2
      layer3  (logits出力、Softmaxなし)
    """
    input_size, hidden_size, output_size = model_params

    class Model(nn.Module):
        def __init__(self, input_size, hidden_size, output_size):
            super().__init__()
            self.layer1 = nn.Linear(input_size, hidden_size)
            self.norm1 = nn.LayerNorm(hidden_size)
            self.layer2 = nn.Linear(hidden_size, hidden_size)
            self.norm2 = nn.LayerNorm(hidden_size)
            self.layer3 = nn.Linear(hidden_size, output_size)
            self.relu = nn.ReLU()
            self.loss_fn = nn.CrossEntropyLoss()

            # LocalTrainer.kt と同一: bias を 0 で初期化
            nn.init.zeros_(self.layer1.bias)
            nn.init.zeros_(self.layer2.bias)
            nn.init.zeros_(self.layer3.bias)

        def forward(self, x):
            x = self.relu(self.norm1(self.layer1(x)))
            x = self.relu(self.norm2(self.layer2(x)))
            x = self.layer3(x)
            return x  # logits を返す (Softmax は呼び出し側で適用)

        @torch.jit.export
        def train_step(self, x, y, lr: float):
            # Forward pass (logits → CrossEntropyLoss)
            logits = self.forward(x)
            loss = self.loss_fn(logits, y)

            # Backward pass
            loss.backward()

            # Manual gradient descent
            with torch.no_grad():
                self.layer1.weight -= lr * self.layer1.weight.grad
                self.layer1.bias -= lr * self.layer1.bias.grad
                self.norm1.weight -= lr * self.norm1.weight.grad
                self.norm1.bias -= lr * self.norm1.bias.grad
                self.layer2.weight -= lr * self.layer2.weight.grad
                self.layer2.bias -= lr * self.layer2.bias.grad
                self.norm2.weight -= lr * self.norm2.weight.grad
                self.norm2.bias -= lr * self.norm2.bias.grad
                self.layer3.weight -= lr * self.layer3.weight.grad
                self.layer3.bias -= lr * self.layer3.bias.grad

                self.layer1.weight.grad.zero_()
                self.layer1.bias.grad.zero_()
                self.norm1.weight.grad.zero_()
                self.norm1.bias.grad.zero_()
                self.layer2.weight.grad.zero_()
                self.layer2.bias.grad.zero_()
                self.norm2.weight.grad.zero_()
                self.norm2.bias.grad.zero_()
                self.layer3.weight.grad.zero_()
                self.layer3.bias.grad.zero_()

            return loss

    # 学習モデルの作成
    trainable_model = Model(*model_params)

    # 学習モデルを保存
    # 記録としてのモデル保存
    torch.save(trainable_model.state_dict(), "initial_model_trainable.pt")
    scripted_model_trainable = torch.jit.script(trainable_model)
    # 端末での使用としてのモデルの保存
    scripted_model_trainable.save("global_initial_mobile.pt")
    print("学習専用モデルを保存しました: global_model_mobile.pt")

    return trainable_model

if __name__ == "__main__":
    create_initial_model(model_params=model_params)
    print("初期モデルの作成と保存が完了しました。")

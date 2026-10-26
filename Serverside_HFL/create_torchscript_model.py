import torch
from torch import nn


model_params = [6,70,3]

def create_initial_model(model_params=model_params):
    # モデル定義
    input_size, hidden_size, output_size = model_params
    class Model(nn.Module):
        def __init__(self, input_size, hidden_size, output_size):
            super().__init__()
            self.layer1 = nn.Linear(input_size, hidden_size)
            self.layer2 = nn.Linear(hidden_size, hidden_size)
            self.layer3 = nn.Linear(hidden_size, output_size)
            self.relu = nn.ReLU()
            self.softmax = nn.Softmax(dim=1)
            self.loss_fn = nn.CrossEntropyLoss()

        def forward(self, x):
            x = self.layer1(x)
            x = self.relu(x)
            x = self.layer2(x)
            x = self.relu(x)
            x = self.layer3(x)
            x = self.softmax(x)
            return x

        @torch.jit.export
        def train_step(self, x, y, lr: float):
            # Forward pass
            pred = self.forward(x)
            loss = self.loss_fn(pred, y)

            # Backward pass
            loss.backward()

            # Manual gradient descent
            with torch.no_grad():
                self.layer1.weight -= lr * self.layer1.weight.grad
                self.layer1.bias -= lr * self.layer1.bias.grad
                self.layer2.weight -= lr * self.layer2.weight.grad
                self.layer2.bias -= lr * self.layer2.bias.grad
                self.layer3.weight -= lr * self.layer3.weight.grad
                self.layer3.bias -= lr * self.layer3.bias.grad

                # Zero gradients
                self.layer1.weight.grad.zero_()
                self.layer1.bias.grad.zero_()
                self.layer2.weight.grad.zero_()
                self.layer2.bias.grad.zero_()
                self.layer3.weight.grad.zero_()
                self.layer3.bias.grad.zero_()

            return loss
        
    # 学習モデルの作成
    trainable_model = Model(*model_params)

    # 学習モデルを保存
    #記録としてのモデル保存
    torch.save(trainable_model.state_dict(), "initial_model_trainable.pt")
    scripted_model_trainable = torch.jit.script(trainable_model)
    #端末でのしようとしてのモデルの保存
    scripted_model_trainable.save("global_initial_mobile.pt")
    print("学習専用モデルを保存しました: global_model_mobile.pt")
    
    return trainable_model

if __name__ == "__main__":
    create_initial_model(model_params=model_params)
    print("初期モデルの作成と保存が完了しました。")

    
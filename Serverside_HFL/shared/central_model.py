import hashlib

import torch
import torch.nn.functional as F


def state_sha256(sd) -> str:
    """重みの float32（リトルエンディアン）をキー名ソート順に連結した SHA256。.pt のバイト列には依存しない。"""
    h = hashlib.sha256()
    for k in sorted(sd):
        h.update(sd[k].detach().cpu().numpy().astype("<f4").tobytes())
    return h.hexdigest()


def forward_scores(sd, features) -> list:
    """端末 MLP と同じ順（Linear→LayerNorm→ReLU を 2 回→Linear）で推論し、各クラス（AP）の softmax 確率を返す。"""
    x = torch.tensor([features], dtype=torch.float32)
    with torch.no_grad():
        h = F.relu(F.layer_norm(F.linear(x, sd["layer1.weight"], sd["layer1.bias"]), (sd["norm1.weight"].numel(),),
                                sd["norm1.weight"], sd["norm1.bias"], eps=1e-5))
        h = F.relu(F.layer_norm(F.linear(h, sd["layer2.weight"], sd["layer2.bias"]), (sd["norm2.weight"].numel(),),
                                sd["norm2.weight"], sd["norm2.bias"], eps=1e-5))
        logits = F.linear(h, sd["layer3.weight"], sd["layer3.bias"])
        return F.softmax(logits, dim=1)[0].tolist()

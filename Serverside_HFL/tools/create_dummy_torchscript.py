import time
from pathlib import Path
import torch
import torch.nn as nn

ts = time.strftime("%Y%m%d_%H%M%S")
base = Path(__file__).resolve().parents[1] / "received_files" / ts
base.mkdir(parents=True, exist_ok=True)

class DummyModel(nn.Module):
    def __init__(self, in_dim=6, hid=16, out_dim=1):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, hid),
            nn.ReLU(),
            nn.Linear(hid, out_dim),
        )
    def forward(self, x):
        return self.net(x)

m = DummyModel()
# example input
import torch
example = torch.randn(1, 6)
traced = torch.jit.trace(m, example)
outfile = base / "global_model_mobile.pt"
traced.save(str(outfile))
print("WROTE", outfile)
print(ts)

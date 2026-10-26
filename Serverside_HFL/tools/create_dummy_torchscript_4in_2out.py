import os
import torch
import torch.nn as nn

def main():
    torch.manual_seed(42)
    model = nn.Sequential(
        nn.Linear(4, 16),
        nn.ReLU(),
        nn.Linear(16, 2)
    )
    example = torch.randn(1, 4)
    ts = torch.jit.trace(model, example)

    out_dir = os.path.join('received_files', '20251218_005125')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'global_model_mobile.pt')
    ts.save(out_path)
    print('WROTE', out_path)

if __name__ == '__main__':
    main()

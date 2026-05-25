import os
import json
import struct
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "sample_files"
OUT.mkdir(exist_ok=True)

# Create weight.bin with two tensors: layer1.weight (2x3 floats), layer2.bias (3 floats)
layer1 = [0.1, 0.2, 0.3,
          0.4, 0.5, 0.6]  # 6 floats
layer2 = [0.01, -0.02, 0.03]  # 3 floats

with open(OUT / "weight.bin", "wb") as f:
    # layer1 offset 0
    off1 = f.tell()
    for v in layer1:
        f.write(struct.pack('<f', v))
    # layer2 offset
    off2 = f.tell()
    for v in layer2:
        f.write(struct.pack('<f', v))

weights_path = OUT / "weight.bin"
weights_size = weights_path.stat().st_size

# Compute sha256
h = hashlib.sha256()
with open(weights_path, 'rb') as f:
    for chunk in iter(lambda: f.read(65536), b''):
        h.update(chunk)
weights_sha = h.hexdigest()

meta = {
    "format_version": 1,
    "model_version": "sample_1",
    "framework": "pytorch_state_dict",
    "dtype": "float32",
    "endianness": "little",
    "weights_size": weights_size,
    "weights_sha256": weights_sha,
    "created_at": "2025-12-17T00:00:00Z",
    "tensors": [
        {
            "name": "layer1.weight",
            "shape": [2,3],
            "offset": off1,
            "length_bytes": 6 * 4,
            "dtype": "float32",
            "order": "C"
        },
        {
            "name": "layer2.bias",
            "shape": [3],
            "offset": off2,
            "length_bytes": 3 * 4,
            "dtype": "float32",
            "order": "C"
        }
    ]
}

with open(OUT / "meta.json", "w", encoding="utf-8") as f:
    json.dump(meta, f, indent=2, ensure_ascii=False)

# Prepare latest_data.csv: try to copy from Serverside if available
serverside_path = Path("C:/Users/tetsu/Serverside/training_data/latest_data.csv")
if serverside_path.exists():
    import shutil
    shutil.copy2(serverside_path, OUT / "latest_data.csv")
    source = str(serverside_path)
else:
    # create small CSV
    with open(OUT / "latest_data.csv", "w", encoding="utf-8") as f:
        f.write("tpNeed,rttNeed,appNum,combi\n")
        for i in range(1,11):
            f.write(f"{i*10},{100+i},{i%4},{i%3}\n")
    source = "generated_dummy"

# compute sha256 and size for data
data_path = OUT / "latest_data.csv"
h2 = hashlib.sha256()
with open(data_path, 'rb') as f:
    for chunk in iter(lambda: f.read(65536), b''):
        h2.update(chunk)
data_sha = h2.hexdigest()
data_size = data_path.stat().st_size

# print results
print("Generated sample files in:", OUT)
print()
print("weight.bin:")
print("  path:", weights_path)
print("  size:", weights_size)
print("  sha256:", weights_sha)
print()
print("meta.json:")
print("  path:", OUT / "meta.json")
print("  content summary: format_version={}, model_version={}".format(meta['format_version'], meta['model_version']))
print()
print("latest_data.csv (source: {}):".format(source))
print("  path:", data_path)
print("  size:", data_size)
print("  sha256:", data_sha)

# Exit successfully


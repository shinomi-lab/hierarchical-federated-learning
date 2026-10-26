#!/usr/bin/env python3
import json, hashlib, struct
from pathlib import Path

out_dir = Path('docs/samples')
out_dir.mkdir(parents=True, exist_ok=True)
# create float32 tensor (shape [2,2])
floats = [1.0, 2.0, 3.0, 4.0]
bin_path = out_dir / 'weight.bin'
with open(bin_path, 'wb') as f:
    for v in floats:
        f.write(struct.pack('<f', v))
# create small latest_data.csv
csv_path = out_dir / 'latest_data.csv'
csv_text = 'x,y\n1,2\n3,4\n'
csv_path.write_text(csv_text, encoding='utf-8')
# compute sha256 and size
def sha256_of(path: Path):
    h = hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest(), path.stat().st_size
bin_sha, bin_size = sha256_of(bin_path)
csv_sha, csv_size = sha256_of(csv_path)
# build meta.json
meta = {
    'format_version': 1,
    'model_version': 'sample-001',
    'framework': 'pytorch_state_dict',
    'dtype': 'float32',
    'endianness': 'little',
    'weights_size': bin_size,
    'weights_sha256': bin_sha,
    'created_at': '2025-12-17T12:00:00Z',
    'tensors': [
        {'name': 'layer.weight', 'shape': [2,2], 'offset': 0, 'length_bytes': 4*4, 'dtype': 'float32', 'order': 'C'}
    ]
}
meta_path = out_dir / 'meta.json'
meta_path.write_text(json.dumps(meta, indent=2), encoding='utf-8')
print('CREATED', bin_path, meta_path, csv_path)
print('BIN size,sha256:', bin_size, bin_sha)
print('CSV size,sha256:', csv_size, csv_sha)

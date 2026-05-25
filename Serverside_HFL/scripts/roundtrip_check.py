import json, numpy as np, torch, sys, pathlib

if len(sys.argv) < 3:
    print("Usage: python scripts/roundtrip_check.py <out_dir> <original_pt>")
    sys.exit(2)

out = pathlib.Path(sys.argv[1])  # e.g. /tmp/test_out
pt_in = pathlib.Path(sys.argv[2])  # original .pt

meta_f = out / "meta.json"
bin_f = out / "weight.bin"
if not meta_f.exists() or not bin_f.exists():
    print("meta.json or weight.bin not found in", out)
    sys.exit(2)

meta = json.loads(meta_f.read_text(encoding="utf-8"))
buf = bin_f.read_bytes()
recon = {}
for t in meta.get('tensors', []):
    off = int(t['offset']); ln = int(t['length_bytes'])
    shape = tuple(t['shape'])
    chunk = memoryview(buf)[off:off+ln]
    arr = np.frombuffer(chunk, dtype='<f4')
    try:
        arr = arr.reshape(shape)
    except Exception:
        print(f"Failed to reshape tensor {t['name']} to {shape}")
        continue
    recon[t['name']] = torch.from_numpy(arr.copy())

# load original
orig_obj = torch.load(str(pt_in), map_location='cpu')
if hasattr(orig_obj, 'state_dict') and not isinstance(orig_obj, dict):
    try:
        orig_sd = orig_obj.state_dict()
    except Exception:
        # fallback: try named_parameters
        try:
            orig_sd = {k: v for k, v in orig_obj.named_parameters()}
        except Exception:
            raise RuntimeError("can't extract state_dict from original .pt")
elif isinstance(orig_obj, dict):
    orig_sd = orig_obj
else:
    # try named_parameters for ScriptModule
    try:
        orig_sd = {k: v for k, v in orig_obj.named_parameters()}
    except Exception:
        raise RuntimeError("can't extract state_dict from original .pt")

# compare keys/shapes/NaN/values
keys = sorted(set(list(recon.keys()) + list(orig_sd.keys())))
for k in keys:
    a = orig_sd.get(k)
    b = recon.get(k)
    if a is None:
        print("MISSING IN ORIGINAL:", k)
        continue
    if b is None:
        print("MISSING IN RECONSTRUCTED:", k)
        continue
    a = a.float().cpu()
    b = b.float().cpu()
    if a.shape != b.shape:
        print("SHAPE MISMATCH", k, a.shape, b.shape)
        continue
    if torch.isnan(b).any() or torch.isinf(b).any():
        print("NaN/Inf in recon for", k)
    diff = (a - b).abs().max().item()
    print(k, "max_abs_diff=", diff)

# meta-level nan info
if meta.get('nan_detected'):
    print('\nmeta.json indicates NaN detected during conversion. nan_keys:', meta.get('nan_keys', []))

print('\nRound-trip check complete')

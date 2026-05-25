#!/usr/bin/env python3
"""
tools/pt_to_meta_weights.py

Minimal utility to convert a PyTorch state_dict (.pt) into a meta.json + weight.bin
suitable for distribution to lightweight clients.

Usage:
  python tools/pt_to_meta_weights.py /path/to/global_model.pt /out/dir --model-version v1

Output:
  /out/dir/weight.bin
  /out/dir/meta.json

Notes:
- This tool attempts to load a state_dict via torch.load(). If the loaded object is not a
  dict, it will try to call `.state_dict()` on the object to obtain a mapping.
- Tensors are written as contiguous float32 little-endian raw bytes in deterministic order
  (sorted by key name).
"""
from __future__ import annotations
import argparse
import json
import time
from pathlib import Path
import hashlib
from typing import Dict, Any, List

import torch
import numpy as np


def sha256_hex(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def load_state_dict(pt_path: Path) -> Dict[str, Any]:
    # Try torch.load first; if it fails because the file is a TorchScript archive,
    # fall back to torch.jit.load to obtain a ScriptModule.
    try:
        obj = torch.load(str(pt_path), map_location="cpu")
    except Exception as e:
        msg = str(e)
        # Common torch error suggests using torch.jit.load for TorchScript archives
        if "TorchScript archive" in msg or "torch.jit.load" in msg or "weights_only" in msg:
            try:
                print("torch.load failed, attempting torch.jit.load() fallback...", msg)
                obj = torch.jit.load(str(pt_path), map_location="cpu")
            except Exception as e2:
                raise RuntimeError(f"Failed to load model via torch.load and torch.jit.load: {e}; {e2}") from e2
        else:
            raise
    if isinstance(obj, dict):
        return obj
    # If it's a module-like object, try to extract state_dict
    try:
        if hasattr(obj, "state_dict"):
            sd = obj.state_dict()
            if isinstance(sd, dict):
                return sd
    except Exception:
        pass
    # Last resort: if it is a ScriptModule, try to call named_parameters
    try:
        params = {k: v for k, v in obj.named_parameters()}
        # also include buffers
        try:
            buffers = {k: v for k, v in obj.named_buffers()}
            params.update(buffers)
        except Exception:
            pass
        if params:
            return params
    except Exception:
        pass
    raise RuntimeError("Unable to obtain state_dict from provided .pt file")


def tensors_to_flat_bytes(sd: Dict[str, Any]) -> (bytes, List[Dict[str, Any],]):
    # LocalTrainer.kt の loadFromFlat と完全一致する順序で書き出す。
    # sorted() はアルファベット順になり loadFromFlat の期待順序と異なるため使わない。
    _CANONICAL_ORDER = [
        "layer1.weight", "layer1.bias",
        "norm1.weight",  "norm1.bias",
        "layer2.weight", "layer2.bias",
        "norm2.weight",  "norm2.bias",
        "layer3.weight", "layer3.bias",
    ]
    # canonical order にあるキーを先に、残りを sorted で末尾に追加
    ordered = [k for k in _CANONICAL_ORDER if k in sd]
    remaining = sorted(k for k in sd if k not in set(ordered))
    keys = ordered + remaining
    offset = 0
    entries: List[Dict[str, Any]] = []
    parts: List[bytes] = []
    nan_keys: List[str] = []
    for k in keys:
        t = sd[k]
        if not hasattr(t, "numpy") and not hasattr(t, "contiguous"):
            # maybe it's a Python number; convert to tensor
            try:
                t = torch.tensor(t)
            except Exception:
                raise RuntimeError(f"Unsupported value for key {k}: {type(t)}")
        # ensure tensor
        tt = t
        if not isinstance(tt, torch.Tensor):
            tt = torch.as_tensor(tt)
        # convert to float32 if floating point, else keep numeric types as float32 for simplicity
        if torch.is_floating_point(tt):
            tt = tt.to(dtype=torch.float32)
        else:
            try:
                tt = tt.to(dtype=torch.float32)
            except Exception:
                # fallback: attempt numpy conversion
                tt = torch.as_tensor(tt, dtype=torch.float32)
        arr = tt.contiguous().cpu().numpy()
        # detect NaN/Inf and sanitize (log via stdout but do not abort)
        try:
            if np.isnan(arr).any() or np.isinf(arr).any():
                print(f"[pt_to_meta_weights] NaN/Inf detected in tensor '{k}', replacing with 0.0 and continuing")
                nan_keys.append(k)
                # replace NaN/Inf with zeros to avoid propagating NaN
                arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        except Exception:
            # best-effort only; continue if numpy checks fail
            pass
        # ensure little-endian float32
        if arr.dtype != np.float32:
            arr = arr.astype(np.float32)
        if arr.dtype.byteorder == ">" or (arr.dtype.byteorder == "=" and not (np.little_endian)):
            arr = arr.byteswap().newbyteorder()
        b = arr.tobytes()
        l = len(b)
        parts.append(b)
        entries.append({
            "name": k,
            "shape": list(arr.shape),
            "offset": offset,
            "length_bytes": l,
        })
        offset += l
    flat = b"".join(parts)
    return flat, entries, nan_keys


def write_outputs(out_dir: Path, weight_bytes: bytes, entries: List[Dict[str, Any]], model_version: str, nan_keys: List[str] | None = None):
    out_dir.mkdir(parents=True, exist_ok=True)
    # Write to temp files and atomically rename into place to avoid partial visibility.
    wpath = out_dir / "weight.bin"
    mpath = out_dir / "meta.json"
    wtmp = out_dir / "weight.bin.tmp"
    mtmp = out_dir / "meta.json.tmp"
    # write binary tmp
    with open(wtmp, "wb") as wf:
        wf.write(weight_bytes)
        try:
            wf.flush()
            import os
            os.fsync(wf.fileno())
        except Exception:
            pass
    # prepare meta content
    meta = {
        "format_version": 1,
        "framework": "pytorch_android",
        "model_version": model_version or time.strftime("%Y%m%d_%H%M%S"),
        "dtype": "float32",
        "endianness": "little",
        "tensors": entries,
        "weights_sha256": sha256_hex(weight_bytes),
        "weights_size": len(weight_bytes),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nan_detected": bool(nan_keys),
        "nan_keys": nan_keys or [],
    }
    with open(mtmp, "w", encoding="utf-8") as mf:
        mf.write(json.dumps(meta, indent=2, ensure_ascii=False))
        try:
            mf.flush()
            import os
            os.fsync(mf.fileno())
        except Exception:
            pass

    # atomic replace
    try:
        mtmp.replace(mpath)
    except Exception:
        import os as _os
        _os.replace(str(mtmp), str(mpath))
    try:
        wtmp.replace(wpath)
    except Exception:
        import os as _os
        _os.replace(str(wtmp), str(wpath))

    return wpath, mpath


def main():
    p = argparse.ArgumentParser()
    p.add_argument("pt", type=Path, help="Path to input .pt (state_dict or model)")
    p.add_argument("out_dir", type=Path, help="Directory to write weight.bin and meta.json")
    p.add_argument("--model-version", type=str, default="", help="Optional model_version string for meta.json")
    args = p.parse_args()

    pt = args.pt
    out = args.out_dir
    if not pt.exists():
        print(f"Input file not found: {pt}")
        raise SystemExit(2)
    print(f"Loading {pt} ...")
    sd = load_state_dict(pt)
    print(f"Loaded state_dict with {len(sd)} keys")
    weight_bytes, entries, nan_keys = tensors_to_flat_bytes(sd)
    print(f"Flattened weights: {len(weight_bytes)} bytes, tensors: {len(entries)}")
    wpath, mpath = write_outputs(out, weight_bytes, entries, args.model_version, nan_keys)
    print(f"Wrote: {wpath}\nWrote: {mpath}")


if __name__ == "__main__":
    main()

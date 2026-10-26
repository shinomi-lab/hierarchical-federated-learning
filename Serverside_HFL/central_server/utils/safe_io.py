from __future__ import annotations

import hashlib
from pathlib import Path
import json
import torch
from typing import Dict, Any


def _sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def safe_save_state_dict(target: Path, sd: Dict[str, Any]) -> Path:
    """Safely save a state_dict to `target`.

    Steps:
    - write to a tmp file next to `target`
    - validate by loading with `torch.load(..., weights_only=True)` to ensure it's a state_dict
    - if valid, atomically replace target
    - return the final path written (target)

    This avoids overwriting existing canonical files with invalid/unknown artifacts.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    # write
    torch.save(sd, str(tmp))

    # validate
    try:
        loaded = torch.load(str(tmp), map_location="cpu", weights_only=True)
        if not isinstance(loaded, dict) or not loaded:
            raise RuntimeError("validation failed: not a non-empty dict")
    except TypeError:
        # Older PyTorch versions may not accept weights_only kw; try safe default
        loaded = torch.load(str(tmp), map_location="cpu")
        if not isinstance(loaded, dict) or not loaded:
            raise RuntimeError("validation failed: not a non-empty dict (fallback)")

    # compute checksum and store .meta alongside for traceability
    try:
        digest = _sha256_hex(tmp)
        meta = {"sha256": digest}
        meta_path = target.with_suffix(target.suffix + ".meta.json")
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    # atomic replace
    tmp.replace(target)
    return target

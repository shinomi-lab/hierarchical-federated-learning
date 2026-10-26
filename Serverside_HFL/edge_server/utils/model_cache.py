from typing import Dict
import asyncio
from pathlib import Path
from .model_loader import load_torch_artifact, build_model_from_state_dict
import asyncio

_cache: Dict[str, Dict] = {}
_global_lock = asyncio.Lock()

async def get_or_load(path: str, model_ctor=None):
    """Return loaded model. If state_dict is found and model_ctor provided, build model."""
    async with _global_lock:
        entry = _cache.get(path)
        if entry:
            entry["ref"] += 1
            return entry["model"]
        # mark as loading
        _cache[path] = {"model": None, "ref": 1, "lock": asyncio.Lock()}
    # load outside global lock
    try:
        async with _cache[path]["lock"]:
            obj = await asyncio.to_thread(load_torch_artifact, path)
            model = None
            if isinstance(obj, dict):
                if model_ctor is None:
                    raise RuntimeError("state_dict loaded but no model_ctor provided")
                model = await asyncio.to_thread(build_model_from_state_dict, obj, model_ctor)
            else:
                model = obj
            _cache[path]["model"] = model
            return model
    except Exception:
        # cleanup on failure
        async with _global_lock:
            _cache.pop(path, None)
        raise

async def release(path: str):
    async with _global_lock:
        entry = _cache.get(path)
        if not entry:
            return
        entry["ref"] -= 1
        if entry["ref"] <= 0:
            # remove and let GC handle
            _cache.pop(path, None)

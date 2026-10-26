"""
Marker backend abstraction.

This module provides a small facade for marker operations used by endpoints.
If `REDIS_URL` is set and the `redis` package is available, it will use Redis
for higher concurrency. Otherwise it falls back to the sqlite-backed
`central_server.utils.marker_store` implementation.

Provided functions:
 - get_edge_round_sha(round_id, edge_id, federation_run_id=None) -> Optional[str]
 - set_edge_round_sha(round_id, edge_id, sha, federation_run_id=None) -> bool
 - has_received_hash(sha) -> bool
 - add_received_hash(sha) -> bool

This keeps the rest of the code agnostic to the underlying store and makes
it easier to migrate to Redis/Postgres in future.
"""
from typing import Optional
import os
import importlib
REDIS_URL = os.environ.get("REDIS_URL")
_redis_client = None
if REDIS_URL:
    try:
        redis_mod = importlib.import_module('redis')
        _redis_client = redis_mod.Redis.from_url(REDIS_URL)
    except Exception:
        # redis not available or connection error; fall back
        _redis_client = None


def _use_redis() -> bool:
    return _redis_client is not None


if not _use_redis():
    # fallback to sqlite marker_store
    from central_server.utils import marker_store as _ms


def _norm_fed_run(federation_run_id: Optional[str]) -> str:
    if federation_run_id is None:
        return "default"
    s = str(federation_run_id).strip()
    return s if s else "default"


def get_edge_round_sha(round_id: int, edge_id: str, federation_run_id: Optional[str] = None) -> Optional[str]:
    if _use_redis():
        fr = _norm_fed_run(federation_run_id)
        key = f"received_by_edge:{fr}:{int(round_id)}"
        try:
            v = _redis_client.hget(key, edge_id)
            return v.decode('utf-8') if v is not None else None
        except Exception:
            return None
    else:
        return _ms.get_edge_round_sha(round_id, edge_id, federation_run_id)


def set_edge_round_sha(round_id: int, edge_id: str, sha: Optional[str] = None, federation_run_id: Optional[str] = None) -> bool:
    if _use_redis():
        fr = _norm_fed_run(federation_run_id)
        key = f"received_by_edge:{fr}:{int(round_id)}"
        try:
            if sha is None:
                # delete field
                _redis_client.hdel(key, edge_id)
            else:
                _redis_client.hset(key, edge_id, sha)
            return True
        except Exception:
            return False
    else:
        return _ms.set_edge_round_sha(round_id, edge_id, sha, federation_run_id)


def has_received_hash(sha: str) -> bool:
    if _use_redis():
        try:
            return _redis_client.sismember("received_hashes", sha)
        except Exception:
            return False
    else:
        return _ms.has_received_hash(sha)


def add_received_hash(sha: str) -> bool:
    if _use_redis():
        try:
            # returns 1 if added, 0 if already present
            res = _redis_client.sadd("received_hashes", sha)
            return res == 1
        except Exception:
            return False
    else:
        return _ms.add_received_hash(sha)

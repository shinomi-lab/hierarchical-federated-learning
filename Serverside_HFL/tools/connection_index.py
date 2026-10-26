import sqlite3
import time
import os
from typing import Optional, Dict

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "connection_index.db")

def _ensure_dir():
    d = os.path.dirname(DB_PATH)
    if not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def init_db():
    _ensure_dir()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS connections(
            device_id TEXT PRIMARY KEY,
            edge_id TEXT,
            endpoint TEXT,
            last_seen INTEGER,
            state TEXT,
            model_version TEXT
        )
        """
        )
        conn.commit()


def register(device_id: str, edge_id: str, endpoint: str, model_version: Optional[str] = None):
    now = int(time.time())
    init_db()
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        conn.execute(
            """
        INSERT INTO connections(device_id,edge_id,endpoint,last_seen,state,model_version)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(device_id) DO UPDATE SET
            edge_id=excluded.edge_id,
            endpoint=excluded.endpoint,
            last_seen=excluded.last_seen,
            state=excluded.state,
            model_version=excluded.model_version
        """,
            (device_id, edge_id, endpoint, now, "connected", model_version),
        )
        conn.commit()


def heartbeat(device_id: str):
    now = int(time.time())
    init_db()
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        conn.execute("UPDATE connections SET last_seen=? WHERE device_id=?", (now, device_id))
        conn.commit()


def deregister(device_id: str):
    init_db()
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        conn.execute("DELETE FROM connections WHERE device_id=?", (device_id,))
        conn.commit()


def whereis(device_id: str, ttl: int = 90) -> Optional[Dict]:
    init_db()
    now = int(time.time())
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        row = conn.execute(
            "SELECT edge_id,endpoint,last_seen,state,model_version FROM connections WHERE device_id=?",
            (device_id,),
        ).fetchone()
    if not row:
        return None
    edge_id, endpoint, last_seen, state, model_version = row
    if now - last_seen > ttl:
        return None
    return {
        "edge_id": edge_id,
        "endpoint": endpoint,
        "last_seen": last_seen,
        "state": state,
        "model_version": model_version,
    }


def cleanup(ttl: int = 90):
    init_db()
    cutoff = int(time.time()) - ttl
    with sqlite3.connect(DB_PATH, timeout=5) as conn:
        conn.execute("DELETE FROM connections WHERE last_seen<?", (cutoff,))
        conn.commit()

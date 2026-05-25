from pathlib import Path
import sqlite3
import threading
from typing import Optional
import os


def _normalize_federation_run(federation_run_id: Optional[str]) -> str:
    """Namespace for per-edge-round markers; aligns with central edge_update Form run_id."""
    if federation_run_id is None:
        return "default"
    s = str(federation_run_id).strip()
    return s if s else "default"


def _migrate_received_by_edge_schema(conn: sqlite3.Connection) -> None:
    """Add federation_run_id to PK when DB was created with legacy (round, edge_id) only."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='received_by_edge'")
    if cur.fetchone() is None:
        cur.execute(
            """
            CREATE TABLE received_by_edge (
                federation_run_id TEXT NOT NULL,
                round INTEGER NOT NULL,
                edge_id TEXT NOT NULL,
                sha TEXT,
                PRIMARY KEY (federation_run_id, round, edge_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_received_by_edge_round ON received_by_edge(round)")
        conn.commit()
        return
    cur.execute("PRAGMA table_info(received_by_edge)")
    cols = [row[1] for row in cur.fetchall()]
    if "federation_run_id" in cols:
        return
    cur.execute(
        """
        CREATE TABLE received_by_edge_new (
            federation_run_id TEXT NOT NULL,
            round INTEGER NOT NULL,
            edge_id TEXT NOT NULL,
            sha TEXT,
            PRIMARY KEY (federation_run_id, round, edge_id)
        )
        """
    )
    cur.execute(
        """
        INSERT INTO received_by_edge_new (federation_run_id, round, edge_id, sha)
        SELECT 'default', round, edge_id, sha FROM received_by_edge
        """
    )
    cur.execute("DROP TABLE received_by_edge")
    cur.execute("ALTER TABLE received_by_edge_new RENAME TO received_by_edge")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_received_by_edge_round ON received_by_edge(round)")
    conn.commit()

# Simple SQLite-backed marker store for received hashes and per-edge-per-round markers.
# Uses INSERT OR IGNORE for idempotent writes and tiny transactions for concurrency.

_DB_PATH = Path(__file__).resolve().parent.parent / "state" / "markers.db"
_INIT_LOCK = threading.Lock()
_initialized = False


def _get_conn():
    """Return a new sqlite3 connection configured for reasonable concurrency.

    Uses a larger timeout and returns an autocommit connection (isolation_level=None).
    Each connection will also set a busy timeout PRAGMA to help with short contention.
    """
    # allow overriding DB path via env var for deployments that want DB outside repo/OneDrive
    env_db = os.environ.get("MARKER_DB_PATH")
    db_path = Path(env_db).expanduser().resolve() if env_db else _DB_PATH
    # ensure parent exists (caller might rely on it)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    # increase timeout to 30s to tolerate transient contention
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    try:
        cur = conn.cursor()
        # busy_timeout in milliseconds
        cur.execute("PRAGMA busy_timeout = 30000")
        # use memory temp store to reduce disk I/O for temp tables
        cur.execute("PRAGMA temp_store = MEMORY")
    except Exception:
        # best-effort; if PRAGMA fails, continue with the connection
        pass
    return conn


import logging as _logging
_db_logger = _logging.getLogger("marker_store")

# 現在のスキーマバージョン
_SCHEMA_VERSION = 2

def _ensure_schema_version(conn: sqlite3.Connection) -> int:
    """スキーマバージョンテーブルを作成し、現在のバージョンを返す。"""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("SELECT version FROM schema_version WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO schema_version (id, version) VALUES (1, ?)", (_SCHEMA_VERSION,))
        conn.commit()
        return _SCHEMA_VERSION
    return row[0]

def _update_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.cursor().execute(
        "UPDATE schema_version SET version = ?, updated_at = datetime('now') WHERE id = 1",
        (version,)
    )
    conn.commit()


def init_marker_db(db_path: Optional[Path] = None):
    global _initialized, _DB_PATH
    if db_path:
        _DB_PATH = db_path
    if _initialized:
        return
    with _INIT_LOCK:
        if _initialized:
            return
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # if MARKER_DB_PATH env var is present, allow init to use that
        env_db = os.environ.get("MARKER_DB_PATH")
        if env_db:
            try:
                _DB_PATH = Path(env_db).expanduser().resolve()
                _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                # ignore and use default
                pass
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            # busy timeout also useful when initializing
            cur.execute("PRAGMA busy_timeout = 30000")
            # Improve write concurrency: less strict sync for higher throughput
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS received_hashes (
                    sha TEXT PRIMARY KEY
                )
                """
            )
            conn.commit()
            # スキーマバージョン管理
            db_version = _ensure_schema_version(conn)
            if db_version < _SCHEMA_VERSION:
                _db_logger.info(f"DBスキーマをバージョン {db_version} → {_SCHEMA_VERSION} にマイグレーション中")
            if db_version > _SCHEMA_VERSION:
                _db_logger.warning(
                    f"DBスキーマバージョン ({db_version}) がコード ({_SCHEMA_VERSION}) より新しいです。"
                    "互換性の問題が発生する可能性があります。"
                )
        finally:
            conn.close()
        # Migrate to (federation_run_id, round, edge_id) when upgrading from legacy schema
        conn2 = _get_conn()
        try:
            _migrate_received_by_edge_schema(conn2)
            # バージョン更新
            _update_schema_version(conn2, _SCHEMA_VERSION)
        finally:
            conn2.close()
        conn3 = _get_conn()
        try:
            cur3 = conn3.cursor()
            cur3.execute("CREATE INDEX IF NOT EXISTS idx_received_by_edge_round ON received_by_edge(round)")
            conn3.commit()
        except Exception:
            pass
        finally:
            conn3.close()
        _initialized = True


def migrate_marker_db(new_path: Path) -> bool:
    """Atomically move existing marker DB file to a new path and reinitialize.

    Returns True on success, False otherwise. This is a best-effort helper to allow
    moving the DB out of OneDrive or other problematic locations.
    """
    init_marker_db()
    global _DB_PATH, _initialized
    try:
        new_path = new_path.expanduser().resolve()
        new_path.parent.mkdir(parents=True, exist_ok=True)
        # close any existing handles by opening and committing a no-op
        conn_old = _get_conn()
        conn_old.close()
        if _DB_PATH.exists():
            tmp = new_path.with_suffix('.db.tmp')
            try:
                _DB_PATH.replace(tmp)
                tmp.replace(new_path)
            except Exception:
                # fallback to copy
                import shutil
                shutil.copy2(str(_DB_PATH), str(new_path))
        # update global path and reinitialize
        _DB_PATH = new_path
        _initialized = False
        init_marker_db()
        return True
    except Exception:
        return False


def has_received_hash(sha: str) -> bool:
    init_marker_db()
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM received_hashes WHERE sha = ?", (sha,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def add_received_hash(sha: str) -> bool:
    init_marker_db()
    conn = _get_conn()
    try:
        cur = conn.cursor()
        # INSERT OR IGNORE returns no rowcount reliably, but we can try and then check
        cur.execute("INSERT OR IGNORE INTO received_hashes (sha) VALUES (?)", (sha,))
        # success if row now exists
        cur.execute("SELECT 1 FROM received_hashes WHERE sha = ?", (sha,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def has_edge_round(round_id: int, edge_id: str, federation_run_id: Optional[str] = None) -> bool:
    init_marker_db()
    fr = _normalize_federation_run(federation_run_id)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM received_by_edge WHERE federation_run_id = ? AND round = ? AND edge_id = ?",
            (fr, int(round_id), edge_id),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def add_edge_round(round_id: int, edge_id: str, sha: Optional[str] = None, federation_run_id: Optional[str] = None) -> bool:
    init_marker_db()
    fr = _normalize_federation_run(federation_run_id)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO received_by_edge (federation_run_id, round, edge_id, sha) VALUES (?, ?, ?, ?)",
            (fr, int(round_id), edge_id, sha),
        )
        cur.execute(
            "SELECT 1 FROM received_by_edge WHERE federation_run_id = ? AND round = ? AND edge_id = ?",
            (fr, int(round_id), edge_id),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def get_edge_round_sha(round_id: int, edge_id: str, federation_run_id: Optional[str] = None) -> Optional[str]:
    """Return the stored sha for (federation_run_id, round, edge_id) if present, otherwise None."""
    init_marker_db()
    fr = _normalize_federation_run(federation_run_id)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT sha FROM received_by_edge WHERE federation_run_id = ? AND round = ? AND edge_id = ?",
            (fr, int(round_id), edge_id),
        )
        r = cur.fetchone()
        if r:
            return r[0]
        return None
    finally:
        conn.close()


def set_edge_round_sha(round_id: int, edge_id: str, sha: Optional[str] = None, federation_run_id: Optional[str] = None) -> bool:
    """Insert or replace the sha for (federation_run_id, round, edge_id). Returns True if present after operation."""
    init_marker_db()
    fr = _normalize_federation_run(federation_run_id)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO received_by_edge (federation_run_id, round, edge_id, sha) VALUES (?, ?, ?, ?)",
            (fr, int(round_id), edge_id, sha),
        )
        cur.execute(
            "SELECT 1 FROM received_by_edge WHERE federation_run_id = ? AND round = ? AND edge_id = ?",
            (fr, int(round_id), edge_id),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def reset_round() -> bool:
    """Reset all round data to start from round 1."""
    init_marker_db()
    conn = _get_conn()
    try:
        cur = conn.cursor()
        # Clear the received_by_edge table
        cur.execute("DELETE FROM received_by_edge")
        conn.commit()
        return True
    except Exception as e:
        print(f"Error resetting rounds: {e}")
        return False
    finally:
        conn.close()

import os
from pathlib import Path

# =========================
# サーバー基本設定
# =========================
EDGE_SERVER_HOST = "0.0.0.0"
EDGE_SERVER_PORT = 8001

# =========================
# 環境プロファイル設定
# =========================
ENV = os.getenv("HFL_ENV", "shinomilab")  # 既定を実機ネットワーク(192.168.11.2)に設定

PROFILES = {
    "hachioji": {
        "CENTRAL_SERVER_URL": "http://192.168.0.11:8000",  # ← PCの実際のIPアドレスに書き換えてください
        "EDGE_URL":           "http://192.168.0.11:8001",  # ← PCの実際のIPアドレスに書き換えてください
    },
    "osaka": {
        "CENTRAL_SERVER_URL": "http://192.168.68.69:8000",
        "EDGE_URL":           "http://192.168.68.69:8001",
    },
    "shinomilab":{
        "CENTRAL_SERVER_URL": "http://192.168.11.2:8000",
        "EDGE_URL":           "http://192.168.11.2:8001",
    },
}

# 不明なENVが指定された場合は既定プロファイル（hachioji）にフォールバック
_PROFILE = PROFILES.get(ENV, PROFILES["hachioji"])
CENTRAL_SERVER_URL = os.getenv("CENTRAL_SERVER_URL", _PROFILE["CENTRAL_SERVER_URL"])
EDGE_URL           = os.getenv("EDGE_URL",           _PROFILE["EDGE_URL"])

# =========================
# 実験パラメータ（複数端末対応）
# =========================
# デフォルトの集約閾値（何台の端末から更新を受け取ったら集約を開始するか）
DEFAULT_AGGREGATION_THRESHOLD = int(os.getenv("EDGE_AGGREGATION_THRESHOLD", "1"))

# 最大同時接続端末数（WebSocket含む）
MAX_CONCURRENT_CLIENTS = int(os.getenv("MAX_CONCURRENT_CLIENTS", "100"))

# 最大リトライ回数（中央サーバーへの送信失敗時）
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# ポーリング間隔（中央サーバーへの問い合わせ間隔、秒）
POLLING_INTERVAL_SECONDS = int(os.getenv("EDGE_POLL_INTERVAL_SECONDS", "5"))

# レートリミッター設定（連続リクエストの最小間隔、秒）
RATE_LIMIT_WINDOW = float(os.getenv("RATE_LIMIT_WINDOW", "0.001"))

# 最大アップロードサイズ（バイト）
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(50 * 1024 * 1024)))  # 50MB

# =========================
# モデル・データファイル設定
# =========================
DEFAULT_MODEL_NAME   = "global_model_mobile.pt"   # ★ 中央/エッジ/端末すべてで同一前提
DEFAULT_CONFIG_NAME  = "app.json"
TRAINING_DATA_FILENAME = "latest_data.csv" # ★ 端末へ配るCSV名（他コードと一致）

# =========================
# ディレクトリ設定
# =========================
BASE_DIR = Path(__file__).resolve().parent.parent
RECEIVED_DIR = BASE_DIR / "received_files"
TERMINAL_UPDATE_DIR = RECEIVED_DIR / "terminal_updates"
AGGREGATION_CACHE_DIR = BASE_DIR / "edge_server" / "_aggregation_cache"

# =========================
# タイムアウト設定（秒）
# =========================
MODEL_DOWNLOAD_TIMEOUT = int(os.getenv("MODEL_DOWNLOAD_TIMEOUT", "30"))
CONFIG_DOWNLOAD_TIMEOUT = int(os.getenv("CONFIG_DOWNLOAD_TIMEOUT", "10"))
TRAINING_DATA_TIMEOUT = int(os.getenv("TRAINING_DATA_TIMEOUT", "60"))
CENTRAL_SERVER_REQUEST_TIMEOUT = int(os.getenv("CENTRAL_SERVER_REQUEST_TIMEOUT", "10"))

# =========================
# バリデーション設定
# =========================
VALID_MODEL_EXTENSIONS = [".pt", ".pth"]
VALID_CONFIG_EXTENSION = ".json"
VALID_DATA_EXTENSION   = ".csv"

# =========================
# エッジサーバー識別子
# =========================
EDGE_SERVER_ID = os.getenv("EDGE_SERVER_ID", "edge-server-01")

# =========================
# Persistence / DB feature flags
# =========================
# If USE_SQLITE_PERSISTENCE is truthy, the system is expected to use a DB
# backend for sent/pending/in_progress state. When enabled, file-based
# persistence on the edge should be disabled unless PERSISTENCE_WRITE_SIDE
# includes 'edge'. This avoids concurrent writes from multiple processes
# and prevents corruption when DB mode is active.
USE_SQLITE_PERSISTENCE = os.getenv("USE_SQLITE_PERSISTENCE", "0").lower() in ("1", "true", "yes")
# Which side is responsible for writing file-backed state when DB mode is enabled.
# Values: 'edge' (edge writes), 'central' (central writes), 'both', 'none'.
PERSISTENCE_WRITE_SIDE = os.getenv("PERSISTENCE_WRITE_SIDE", "edge")
# =========================
# 端末許可リスト（静的マッピング）
# =========================
# フォーマット:
# {
#   "edge-server-01": ["device-001", "device-002"],
#   "edge-server-02": ["device-003", "device-004"],
# }
# 環境変数 `TERMINAL_ALLOWLIST_JSON` が指定されていればそれを優先します。
_ALLOWLIST_DEFAULT = {
    "edge-server-01": [],
    "edge-server-02": [],
}
try:
    import json as _json
    _allowlist_env = os.getenv("TERMINAL_ALLOWLIST_JSON")
    if _allowlist_env:
        TERMINAL_ALLOWLIST = _json.loads(_allowlist_env)
    else:
        TERMINAL_ALLOWLIST = _ALLOWLIST_DEFAULT
except Exception:
    TERMINAL_ALLOWLIST = _ALLOWLIST_DEFAULT

def is_terminal_allowed(terminal_id: str) -> bool:
    try:
        allowed = TERMINAL_ALLOWLIST.get(EDGE_SERVER_ID, [])
        if not allowed:
            # 空なら制限なし
            return True
        return terminal_id in allowed
    except Exception:
        return True

# ===============
# Client log upload
# ===============
# Storage directory for terminal log uploads
TERMINAL_LOGS_DIR = (Path(__file__).resolve().parent.parent / "received_files" / "terminal_logs")
# Per-file size limit for client log uploads (default 10MB)
MAX_LOG_UPLOAD_SIZE_PER_FILE = int(os.getenv("MAX_LOG_UPLOAD_SIZE_PER_FILE", str(10 * 1024 * 1024)))
# Retention policy for terminal logs (days); 0 or negative disables pruning
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))

# WebSocket delay hint to stagger device downloads (milliseconds)
WS_DELAY_HINT_MS = int(os.getenv("WS_DELAY_HINT_MS", "0"))

# =========================
# Inference defaults for edge serving (can be overridden via env)
# =========================
# Number of APs expected (default simulation)
AP_NUM_MAX = int(os.getenv("AP_NUM_MAX", "2"))
# Number of app categories: one-hot width and model output class count
APP_COUNT = int(os.getenv("APP_COUNT", "4"))
# normalization stats (defaults; override with env for real model)
TP_MEAN = float(os.getenv("TP_MEAN", "0.0"))
TP_STD = float(os.getenv("TP_STD", "1.0"))
RTT_MEAN = float(os.getenv("RTT_MEAN", "0.0"))
RTT_STD = float(os.getenv("RTT_STD", "1.0"))
# Base RTT (ms) used for dynamic RTT calculation when simulating congestion
EDGE_BASE_RTT_MS = int(os.getenv("EDGE_BASE_RTT_MS", "20"))
# confidence threshold for fallback
CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.2"))

# ===============
# Terminal telemetry (battery, CPU, etc.)
# ===============
# Storage directory for terminal telemetry JSONL
TERMINAL_TELEMETRY_DIR = (Path(__file__).resolve().parent.parent / "received_files" / "terminal_telemetry")
# Max JSON payload size accepted (bytes)
MAX_TELEMETRY_PAYLOAD_BYTES = int(os.getenv("MAX_TELEMETRY_PAYLOAD_BYTES", str(128 * 1024)))

# ===============
# Client meta constraints
# ===============
# Max UTF-8 bytes for client_meta text part on weights upload
MAX_CLIENT_META_BYTES = int(os.getenv("MAX_CLIENT_META_BYTES", "8192"))

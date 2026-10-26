from pathlib import Path
import os

ROOT_DIR = Path(__file__).resolve().parent.parent

# サーバー設定
CENTRAL_SERVER_HOST = os.getenv("CENTRAL_SERVER_HOST", "0.0.0.0")
CENTRAL_SERVER_PORT = int(os.getenv("CENTRAL_SERVER_PORT", "8000"))

# Detect OneDrive and use external storage to avoid sync delays/locks
def _is_onedrive(p: Path) -> bool:
    try:
        return "onedrive" in str(p).lower() or "sharepoint" in str(p).lower()
    except Exception:
        return False

# Check if HFL_STORAGE_DIR env var is set or if we're in OneDrive
env_storage = os.environ.get("HFL_STORAGE_DIR")
if env_storage:
    STORAGE_ROOT = Path(env_storage).expanduser().resolve()
elif _is_onedrive(ROOT_DIR):
    # Use home directory to avoid OneDrive sync issues
    STORAGE_ROOT = Path.home() / "hfl_data"
else:
    STORAGE_ROOT = ROOT_DIR

STATE_DIR = STORAGE_ROOT / "state"
DIST_DIR = STORAGE_ROOT / "dist"
RECEIVED_EDGES_DIR = STORAGE_ROOT / "received_edges"

# ★ここを「mobile」に統一
DEFAULT_MODEL_NAME = "global_model_mobile.pt"
GLOBAL_MODEL_PATH  = STATE_DIR / DEFAULT_MODEL_NAME

CONFIG_PATH = STATE_DIR / "app.json"

TRAINING_DATA_DIR = STATE_DIR / "training_data"
_source_data_env = os.getenv("SOURCE_DATA_PATH")
SOURCE_DATA_PATH = Path(_source_data_env) if _source_data_env else TRAINING_DATA_DIR / "latest_data.csv"
LATEST_DATA_CSV_PATH = TRAINING_DATA_DIR / "latest_data.csv"

META_PATH = STATE_DIR / "current_meta.json"

DEFAULT_CONFIG_NAME = "app.json"

# 中央サーバーが何台のエッジから更新を受け取ったら集約を開始するかのしきい値
# 1 に設定すると、1台のエッジから更新が来た時点で即時集約される
CENTRAL_AGGREGATION_THRESHOLD = int(os.getenv("CENTRAL_AGGREGATION_THRESHOLD", "1"))

# =========================
# 非同期 HFL モード設定（Central 側）
# =========================
# HFL_MODE: "sync" | "async"
#   sync  = 既存の同期型（全エッジ同ラウンドで集約）
#   async = 閾値ベース非同期型（K_CENTRAL 件そろえば即集約、staleness 許容）
HFL_MODE = os.getenv("HFL_MODE", "sync").lower()

# 非同期時の Central 集約閾値（何台 Edge が届いたらグローバル集約するか）
# 2エッジ構成の実験では K_CENTRAL=2 を推奨（5グローバルラウンド = 5回の集約で揃う）
K_CENTRAL = int(os.getenv("K_CENTRAL", "2"))

# 非同期時に許容する最大 staleness（edge_round が古すぎる場合は破棄）
MAX_STALENESS_ROUNDS_CENTRAL = int(os.getenv("MAX_STALENESS_ROUNDS", "5"))


def is_async_mode() -> bool:
    """非同期 HFL モード（Central 側）が有効かどうか。"""
    return HFL_MODE == "async"

for p in [STATE_DIR, TRAINING_DATA_DIR, DIST_DIR, RECEIVED_EDGES_DIR]:
    p.mkdir(parents=True, exist_ok=True)

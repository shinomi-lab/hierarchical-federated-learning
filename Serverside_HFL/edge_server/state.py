# edge_server/state.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Dict, List, Optional
import json
from pathlib import Path

import torch  # 型ヒント用（実体は dict[str, Tensor] ）

# ============ 更新レコード（1件の端末アップデート） ============
@dataclass
class UpdateRecord:
    terminal_id: str
    state_dict: Optional[Dict[str, torch.Tensor]]  # deferred load 時は None
    n_samples: int
    sig: str                  # 受領バイナリのsha256
    path: str                 # 保存した .pt のパス
    manifest: Optional[str] = None  # manifest.json のパス（任意）
    # 以下は端末側から渡される追加メタ（任意）
    run_id: Optional[str] = None        # 端末セッション/ラン識別子（UUID 等）
    local_seq: Optional[int] = None     # 端末内で単調増加するシーケンス
    started_at: Optional[str] = None    # 端末の run 開始時刻（文字列）
    event_ts: Optional[str] = None      # 端末が送信した更新時刻
    client_meta: Optional[str] = None  # 端末から受け取った client_meta JSON 文字列

# ============ エッジの現在のメタ状態 ============
@dataclass
class EdgeMetaState:
    """エッジサーバーの現在のラウンドやモデルIDを管理する。"""
    round: int = 1
    model_id: str = "demo-mlp-v1"
    base_hash: str = "sha256:bootstrap"
    aggregation_threshold: int = None  # __post_init__で設定
    is_aggregating: bool = False  # 集約処理中かどうか
    waiting_for_new_model: bool = False  # 新しいモデルを待機中かどうか
    
    def __post_init__(self):
        if self.aggregation_threshold is None:
            from edge_server.config import DEFAULT_AGGREGATION_THRESHOLD
            self.aggregation_threshold = DEFAULT_AGGREGATION_THRESHOLD

    def update(self, new_meta: dict):
        """中央サーバから取得したメタ情報で自身を更新する。"""
        old_round = self.round
        self.round = new_meta.get("round", self.round)
        self.model_id = new_meta.get("model_id", self.model_id)
        self.base_hash = new_meta.get("base_hash", self.base_hash)
        self.aggregation_threshold = new_meta.get("aggregation_threshold", self.aggregation_threshold)
        
        # 新しいラウンドを受信したら待機フラグをクリア
        if self.round > old_round:
            self.waiting_for_new_model = False
            logging.info(f"エッジ状態をラウンド {self.round} に更新しました。新端末の受け入れ準備完了")
        else:
            logging.info(f"エッジ状態を更新しました（ラウンド {self.round} のまま）")


# 永続化用関数群はモジュール下部で定義します（current_edge_state の後）

# ============ 共有状態 ============

# ラウンドID -> そのラウンドに届いた更新のリスト
# 例: { 1: [UpdateRecord(...), UpdateRecord(...)] }
terminal_state_dicts: Dict[int, List[UpdateRecord]] = {}

# 既知の端末レジストリ（お好みで利用）
edge_registry: set[str] = set()

# per-terminal ledger persisted per run: maps terminal_id -> {"run_id": str, "last_seq": int, "last_seen": ts}
terminal_ledgers: Dict[str, Dict[str, object]] = {}


def _load_ledgers_from_disk():
    try:
        ledger_path = CURRENT_RUN_DIR / "ledgers.json"
        if ledger_path.exists():
            txt = ledger_path.read_text(encoding="utf-8")
            data = json.loads(txt)
            if isinstance(data, dict):
                terminal_ledgers.clear()
                for k, v in data.items():
                    terminal_ledgers[k] = v
    except Exception:
        logging.exception("端末レジャーのディスク読み込みに失敗しました")


def _save_ledgers_to_disk():
    try:
        ledger_path = CURRENT_RUN_DIR / "ledgers.json"
        tmp = ledger_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(terminal_ledgers, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(ledger_path)
    except Exception:
        logging.exception("端末レジャーのディスク保存に失敗しました")


def record_terminal_ledger(terminal_id: str, run_id: Optional[str], last_seq: Optional[int]):
    try:
        terminal_ledgers.setdefault(terminal_id, {})
        if run_id is not None:
            terminal_ledgers[terminal_id]['run_id'] = run_id
        if last_seq is not None:
            terminal_ledgers[terminal_id]['last_seq'] = int(last_seq)
        terminal_ledgers[terminal_id]['last_seen'] = int(__import__('time').time())
        _save_ledgers_to_disk()
    except Exception:
        logging.exception(f"端末 {terminal_id} のレジャー記録に失敗しました")

# 排他制御ロック（読み書き時に必ず取る）
state_dict_lock = asyncio.Lock()

# エッジのメタ状態のインスタンス
current_edge_state = EdgeMetaState()


# === 永続化ファイルとユーティリティ（実行ごとに分けて保存） ===
# runs_dir/run_<ts>/edge_state.json を使う。起動時は過去ランの最新 state を読み、
# その state に in_progress=True がなければ "cold start" と見なして round を 1 にリセットする。
RUNS_DIR = Path(__file__).resolve().parent / "runs"
RUNS_DIR.mkdir(exist_ok=True)


def _state_to_dict(in_progress: bool = False) -> dict:
    return {
        "round": int(current_edge_state.round),
        "model_id": current_edge_state.model_id,
        "base_hash": current_edge_state.base_hash,
        "aggregation_threshold": int(current_edge_state.aggregation_threshold),
        "in_progress": bool(in_progress),
    }


def save_state(in_progress: bool = False) -> None:
    """Persist current edge meta to disk in the current run directory with an optional in_progress flag."""
    try:
        if not CURRENT_RUN_DIR.exists():
            CURRENT_RUN_DIR.mkdir(parents=True, exist_ok=True)
        d = _state_to_dict(in_progress=in_progress)
        (CURRENT_RUN_DIR / "edge_state.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("エッジ状態のディスク永続化に失敗しました")


def load_state_from_dir(dpath: Path) -> Optional[dict]:
    try:
        sf = dpath / "edge_state.json"
        if sf.exists():
            return json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        logging.exception(f"永続化されたエッジ状態の読み込みに失敗しました {dpath}")
    return None


def set_processing(flag: bool) -> None:
    """Set processing flag and persist state in current run."""
    save_state(in_progress=flag)


# 起動時: 過去の runs ディレクトリから最新の state を読み込んで挙動を決定
def _initialize_run_state():
    global CURRENT_RUN_DIR
    # find latest previous run (by directory name ordering)
    runs = sorted([p for p in RUNS_DIR.iterdir() if p.is_dir()])
    latest = runs[-1] if runs else None
    prev_state = None
    if latest:
        prev_state = load_state_from_dir(latest)

    # 前回状態があればラウンドを含むすべてのメタを引き継ぐ。
    # in_progress フラグの有無に関わらずラウンド番号は常に復元する。
    # 前回状態が存在しない（初回起動）場合のみ round=1 から開始する。
    if prev_state:
        try:
            current_edge_state.round = int(prev_state.get("round", current_edge_state.round))
            current_edge_state.model_id = prev_state.get("model_id", current_edge_state.model_id)
            current_edge_state.base_hash = prev_state.get("base_hash", current_edge_state.base_hash)
            current_edge_state.aggregation_threshold = int(prev_state.get("aggregation_threshold", current_edge_state.aggregation_threshold))
            if prev_state.get("in_progress"):
                logging.info(f"前回の処理中ラン状態を復元しました: round={current_edge_state.round}")
            else:
                logging.info(f"前回の正常終了状態を復元しました: round={current_edge_state.round}")
        except Exception:
            logging.exception("前回状態の適用に失敗しました")
    else:
        current_edge_state.round = 1
        logging.info("初回起動（前回状態なし）。ラウンド 1 から開始します")

    # create a new run directory for this process
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / f"run_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    CURRENT_RUN_DIR = run_dir
    # persist initial state
    save_state(in_progress=False)
    # load ledgers if present
    try:
        _load_ledgers_from_disk()
    except Exception:
        logging.exception("ラン状態初期化後のレジャー読み込みに失敗しました")


# initialize on import
CURRENT_RUN_DIR = RUNS_DIR / "current"
_initialize_run_state()

# ============ 補助ユーティリティ（任意） ============

def bucket_count(round_id: int) -> int:
    """ラウンドの更新件数を返す。"""
    return len(terminal_state_dicts.get(round_id, []))

def total_count() -> int:
    """全ラウンド合計の更新件数を返す。"""
    return sum(len(v) for v in terminal_state_dicts.values())

def clear_round(round_id: int) -> None:
    """集約後にラウンドのバケツをクリア（または外部にアーカイブ）。"""
    terminal_state_dicts[round_id] = []

def append_update(round_id: int, rec: UpdateRecord) -> None:
    """ロック取得済みの前提で、ラウンドのバケツに追加。"""
    terminal_state_dicts.setdefault(round_id, []).append(rec)

class RetryManager:
    def __init__(self):
        self.retry_count = {}  # {round_id: retry_count}

    def should_retry(self, round_id):
        from edge_server.config import MAX_RETRIES
        return self.retry_count.get(round_id, 0) < MAX_RETRIES

    def increment_retry(self, round_id):
        self.retry_count[round_id] = self.retry_count.get(round_id, 0) + 1

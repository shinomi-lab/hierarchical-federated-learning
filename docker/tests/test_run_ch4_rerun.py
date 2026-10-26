import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_ch4_rerun import central_k_values  # noqa: E402

LOG = """\
hfl-central  | 2026-10-14 16:27:05 - central_server - INFO - async_edge_received: {"edge_id": "edge-00", "round": 1, "central_round": 1, "K_CENTRAL": 2}
hfl-central  | 2026-10-14 16:27:06 - central_server.endpoints.edge_update - INFO - [中央 async] K_CENTRAL=2 達成 → ラウンド 1 集約開始
hfl-central  | 2026-10-14 16:27:06 - central_server - INFO - async_aggregation_triggered: {"central_round": 1, "n_edges": 2, "K_CENTRAL": 2}
"""


def test_reads_values_from_json_events():
    assert central_k_values(LOG) == {2}


def test_detects_other_value():
    assert central_k_values(LOG.replace('"K_CENTRAL": 2}', '"K_CENTRAL": 1}', 1)) == {1, 2}


def test_empty_when_no_async_events():
    assert central_k_values("hfl-central  | INFO - sync aggregation\n") == set()

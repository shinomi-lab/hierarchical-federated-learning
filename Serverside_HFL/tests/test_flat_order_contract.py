# Serverside_HFL/tests/test_flat_order_contract.py
import json
from pathlib import Path

from edge_server.endpoints.terminal_update import spec_for_full_mlp


def test_server_flat_spec_matches_terminal_order():
    order_file = Path(__file__).resolve().parents[2] / "HFL_terminals/app/src/test/resources/golden/order.json"
    order = json.loads(order_file.read_text())
    assert [k for k, _ in spec_for_full_mlp(6, 8, 4)] == order

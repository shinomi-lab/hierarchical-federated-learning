"""start.py の推論スナップショットの「現 AP」表示（OpenSpec record-connected-ap 1.4）。

端末が送る virtual_router_id は、固定の HFL_A24 から実際の SSID に変わる。実 SSID でも AP-A/AP-B/? と出ること、
取れないときの unknown が AP-A にならないことを固定する。
"""
import pytest


@pytest.mark.parametrize("router_id,expected", [
    ("HFL_A24", "AP-A"),
    ("HFL_B", "AP-B"),
    ("unknown", "?"),
    ("", "?"),
    (None, "?"),
])
def test_current_ap_from_virtual_router_id(router_id, expected):
    import start  # Serverside_HFL/start.py（import だけでは何も起動しない）

    assert start._current_ap(router_id) == expected

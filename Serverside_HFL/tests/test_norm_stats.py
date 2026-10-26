# Serverside_HFL/tests/test_norm_stats.py
import json
import math

from tools.make_norm_stats import compute


def test_population_stats(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("5,0,0,1\n10,0,1,1\n0,200,2,0\n0,100,3,0\n")
    s = compute(p)
    assert s["rows"] == 4
    assert math.isclose(s["tp_mean"], 3.75) and math.isclose(s["rtt_mean"], 75.0)
    assert math.isclose(s["tp_std"], math.sqrt(((5-3.75)**2 + (10-3.75)**2 + 2 * 3.75**2) / 4))

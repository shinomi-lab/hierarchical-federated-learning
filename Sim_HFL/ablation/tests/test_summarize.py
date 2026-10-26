import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ablation.summarize import fisher_two_sided, summarize  # noqa: E402


class SummarizeTest(unittest.TestCase):
    def test_fisher_known_values(self):
        # 実験 1 の値（手計算と一致）: 3/5 対 0/5 → 0.167、4/10 対 0/10 → 0.087
        self.assertAlmostEqual(fisher_two_sided(3, 2, 0, 5), 0.1667, places=3)
        self.assertAlmostEqual(fisher_two_sided(4, 6, 0, 10), 0.0867, places=3)

    def test_fisher_symmetric_and_bounds(self):
        self.assertAlmostEqual(fisher_two_sided(5, 5, 5, 5), 1.0, places=6)
        self.assertLessEqual(fisher_two_sided(10, 0, 0, 10), 1e-4)

    def test_summarize_counts_collapse(self):
        def r(c, m, sat):
            return {"condition": c, "mode": m, "sat_mean_tail": str(sat), "sat_hm_tail": "0.03",
                    "staleness_mean": "", "staleness_max": ""}
        rows = [r("data_dir0.5", "async", 0.6), r("data_dir0.5", "async", 0.5075),
                r("data_dir0.5", "sync", 0.5075), r("data_dir0.5", "sync", 0.5075)]
        md = summarize(rows)
        self.assertIn("| data_dir0.5 | async | 2 |", md)
        self.assertIn("1/2", md)   # Async の崩壊
        self.assertIn("2/2", md)   # Sync の崩壊

    def test_summarize_tolerates_empty_sat_and_uses_valid_n(self):
        """満足度が空の行（satisfaction_summary が無い試行）があっても落ちず、崩壊の n は有効な値の数（レビュー指摘）。"""
        def r(m, sat):
            return {"condition": "data_dir0.5", "mode": m, "sat_mean_tail": sat, "sat_hm_tail": "0.03",
                    "staleness_mean": "", "staleness_max": ""}
        rows = [r("async", "0.6"), r("async", ""), r("sync", "0.5075"), r("sync", "0.5075")]
        md = summarize(rows)                       # ValueError にならない
        self.assertIn("0/1", md)                   # Async: 有効 1 件のうち崩壊 0
        self.assertIn("2/2", md)


if __name__ == "__main__":
    unittest.main()

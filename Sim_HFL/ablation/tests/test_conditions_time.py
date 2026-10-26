import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ablation import conditions as C  # noqa: E402
from ablation import timemodel as T  # noqa: E402


class ConditionsTest(unittest.TestCase):
    def setUp(self):
        self.conds = C.build_conditions()
        self.base = self.conds[0]["cfg"]

    def test_each_condition_differs_on_one_axis_only(self):
        axis_keys = {
            "speed": {("async", "terminal_speed_std")},
            "data": {("data", "iid"), ("data", "dirichlet_alpha")},
            "ap": {("data", "ap_a", "n_channels")},
            "apmu": {("data", "ap_a", "mu_rtt")},
            "apmuB": {("data", "ap_b", "mu_rtt")},
        }
        for c in self.conds[1:]:
            diff = C.differing_keys(self.base, c["cfg"])
            self.assertTrue(diff, c["name"])
            self.assertTrue(diff <= axis_keys[c["axis"]], (c["name"], diff))

    def test_aligned_condition_exists(self):
        c = next(c for c in self.conds if c["name"] == "speed_0")
        self.assertEqual(c["cfg"]["async"]["terminal_speed_std"], 0.0)
        self.assertTrue(c["cfg"]["data"]["iid"])
        self.assertEqual(C.differing_keys(self.base, c["cfg"]), {("async", "terminal_speed_std")})

    def test_ap_axis_uses_key_both_runners_read(self):
        for c in self.conds:
            self.assertNotIn("background_load", c["cfg"].get("experiment", {}))
        ap = [c for c in self.conds if c["axis"] == "ap"]
        self.assertEqual({c["cfg"]["data"]["ap_a"]["n_channels"] for c in ap}, {1, 4})


    def test_apmu_axis_changes_only_mu_rtt(self):
        apmu = [c for c in self.conds if c["axis"] == "apmu"]
        self.assertEqual({c["cfg"]["data"]["ap_a"]["mu_rtt"] for c in apmu}, {1.0, 4.0})
        for c in apmu:
            self.assertEqual(C.differing_keys(self.base, c["cfg"]), {("data", "ap_a", "mu_rtt")})
            self.assertNotIn("background_load", c["cfg"].get("experiment", {}))


    def test_apmuB_axis_changes_only_ap_b_mu_rtt(self):
        apb = [c for c in self.conds if c["axis"] == "apmuB"]
        self.assertEqual({c["cfg"]["data"]["ap_b"]["mu_rtt"] for c in apb}, {2.5, 10.0})
        for c in apb:
            self.assertEqual(C.differing_keys(self.base, c["cfg"]), {("data", "ap_b", "mu_rtt")})


class TimeModelTest(unittest.TestCase):
    def test_equal_when_std_zero(self):
        for seed in range(20):
            a, s = T.simulate(seed, std=0.0)
            self.assertEqual(a, s)
            self.assertEqual(a, 30)

    def test_sync_not_faster_than_async(self):
        for seed in range(200):
            a, s = T.simulate(seed, std=1.0)
            self.assertGreaterEqual(s, a)

    def test_reproducible(self):
        self.assertEqual(T.simulate(7, std=0.5), T.simulate(7, std=0.5))

    def test_same_draws_for_both_modes(self):
        d = T.durations(3, 4, 10, 1.0, 1.0)
        self.assertEqual(T.completion_steps(d), T.simulate(3, 4, 10, 1.0, 1.0))

    def test_hand_computed(self):
        import numpy as np
        d = np.array([[1, 2, 1], [2, 1, 1]])
        # async: max(4, 4) = 4 ; sync: max per cycle = 2+2+1 = 5
        self.assertEqual(T.completion_steps(d), (4, 5))


if __name__ == "__main__":
    unittest.main()

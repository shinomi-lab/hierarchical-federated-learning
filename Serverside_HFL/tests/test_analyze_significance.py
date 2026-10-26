import math

from scripts.analyze_latest_trial import one_sample_significance, t_p_two_sided


def test_t_p_two_sided_matches_known_values():
    # Welch t=2.45, df=4.1 → 0.069（experiment_results/pvalue_recheck_20261014.md）
    assert abs(t_p_two_sided(2.45, 4.1) - 0.0689) < 5e-4
    # df=4 の両側 5% 臨界値 2.776
    assert abs(t_p_two_sided(2.776, 4) - 0.05) < 1e-3
    assert t_p_two_sided(0.0, 4) == 1.0
    assert t_p_two_sided(math.inf, 4) == 0.0


def test_small_n_is_not_significant_just_because_t_exceeds_2():
    # n=5, t≈2.5 は旧判定（|t|>2）なら有意だが、df=4 では p≈0.067
    deltas = [0.0, 0.05, 0.1, 0.02, 0.08]
    line = one_sample_significance(deltas)
    assert "No" in line and "Yes" not in line
    assert "df=4" in line


def test_large_effect_is_significant():
    line = one_sample_significance([0.10, 0.11, 0.12, 0.10, 0.11])
    assert "Yes (p<0.05)" in line


def test_zero_variance_is_not_tested():
    line = one_sample_significance([0.05, 0.05, 0.05])
    assert "not testable" in line


def test_too_few_values():
    assert one_sample_significance([0.1]) is None

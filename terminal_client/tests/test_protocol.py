import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from protocol import (
    NormStats,
    features,
    load_csv_dataset,
    should_wait_for_round,
    trial_validity,
    upload_form,
)

NS = NormStats(tp_mean=4.0, tp_std=2.0, rtt_mean=100.0, rtt_std=50.0)


def test_features_match_android_zscore_and_onehot():
    x = features(need_tp=8.0, need_rtt=50.0, app_idx=2, ns=NS)
    assert x == pytest.approx([2.0, -1.0, 0.0, 0.0, 1.0, 0.0])


def test_features_reject_unknown_app():
    with pytest.raises(ValueError):
        features(1.0, 1.0, 4, NS)


def test_load_csv_dataset(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("0,200,2,0\n8,50,0,1\n\n")
    ds = load_csv_dataset(str(p), NS)
    assert len(ds) == 2
    x, y = ds[1]
    assert x.tolist() == pytest.approx([2.0, -1.0, 1.0, 0.0, 0.0, 0.0])
    assert int(y) == 1


def test_upload_form_fields():
    f = upload_form(round_id=7, base_hash="abc", n_samples=100)
    assert f["round_id"] == "7" and f["base_hash"] == "abc" and f["n_samples"] == "100"
    assert f["payload_kind"] == "full" and f["dtype"] == "f32_flat"
    assert (f["input_size"], f["hidden_size"], f["output_size"]) == ("6", "32", "2")


def test_wait_only_in_sync():
    assert should_wait_for_round("sync") is True
    assert should_wait_for_round("async") is False
    assert should_wait_for_round(None) is True  # サーバの既定は sync


def _cycles(inits, shas):
    return [{"cycle": i + 1, "init_source": s, "model_sha256": h} for i, (s, h) in enumerate(zip(inits, shas))]


def test_valid_trial():
    t = {"t1": _cycles(["RANDOM", "DOWNLOADED", "DOWNLOADED"], [None, "a", "b"])}
    assert trial_validity(t, num_cycles=3) == (True, "")


def test_invalid_when_not_downloaded_in_sync():
    t = {"t1": _cycles(["RANDOM", "DOWNLOADED", "PROVIDED"], [None, "a", "a"])}
    assert trial_validity(t, num_cycles=3, hfl_mode="sync") == (False, "not_downloaded")


def test_async_allows_provided_but_needs_one_download_per_terminal():
    # async は待たないので、新モデルが来る前のサイクルは自モデル（PROVIDED）で続けるのが正常
    ok = {"t1": _cycles(["RANDOM", "PROVIDED", "DOWNLOADED"], [None, None, "b"]),
          "t2": _cycles(["RANDOM", "DOWNLOADED", "PROVIDED"], [None, "a", "a"])}
    assert trial_validity(ok, num_cycles=3, hfl_mode="async") == (True, "")
    ng = {"t1": _cycles(["RANDOM", "PROVIDED", "PROVIDED"], [None, None, None]),
          "t2": _cycles(["RANDOM", "DOWNLOADED", "DOWNLOADED"], [None, "a", "b"])}
    assert trial_validity(ng, num_cycles=3, hfl_mode="async") == (False, "not_downloaded")


def test_invalid_when_incomplete():
    t = {"t1": _cycles(["RANDOM", "DOWNLOADED"], [None, "a"])}
    assert trial_validity(t, num_cycles=3) == (False, "incomplete")


def test_invalid_when_model_never_changes():
    t = {"t1": _cycles(["DOWNLOADED"] * 3, ["a", "a", "a"]),
         "t2": _cycles(["DOWNLOADED"] * 3, ["a", "a", "a"])}
    assert trial_validity(t, num_cycles=3) == (False, "single_model")


def test_invalid_when_no_terminals():
    assert trial_validity({}, num_cycles=3) == (False, "no_terminals")

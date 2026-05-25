import torch
import pytest

from edge_server.endpoints.aggregation import _fedavg_state_dicts_weighted


def make_sd(val: float):
    return {"w": torch.tensor([val, val], dtype=torch.float32), "b": torch.tensor([val], dtype=torch.float32)}


def test_fedavg_happy_path():
    sd1 = make_sd(1.0)
    sd2 = make_sd(3.0)
    # weights: nsamples 1 and 3 -> normalized weights [0.25, 0.75]
    out = _fedavg_state_dicts_weighted([(sd1, 1), (sd2, 3)])
    assert torch.allclose(out["w"], torch.tensor([2.5, 2.5], dtype=torch.float32))
    assert torch.allclose(out["b"], torch.tensor([2.5], dtype=torch.float32))


def test_fedavg_key_mismatch_raises():
    sd1 = {"w": torch.tensor([1.0])}
    sd2 = {"b": torch.tensor([2.0])}
    with pytest.raises(ValueError):
        _ = _fedavg_state_dicts_weighted([(sd1, 1), (sd2, 1)])

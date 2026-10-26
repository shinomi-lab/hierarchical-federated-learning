# Serverside_HFL/tests/test_initial_model.py
import torch

from tools.make_initial_model import build_state_dict
from edge_server.endpoints.terminal_update import spec_for_full_mlp


def test_initial_model_matches_terminal_layout():
    sd = build_state_dict(input_size=6, hidden_size=32, output_size=2, seed=0)
    assert set(sd) == {k for k, _ in spec_for_full_mlp(6, 32, 2)}   # 端末/サーバ復元仕様と同じ10テンソル
    for k, shape in spec_for_full_mlp(6, 32, 2):
        assert tuple(sd[k].shape) == shape
    assert torch.allclose(sd["norm1.weight"], torch.ones(32))   # LayerNorm 既定値
    assert build_state_dict(6, 32, 2, seed=0)["layer1.weight"].equal(sd["layer1.weight"])   # 決定的

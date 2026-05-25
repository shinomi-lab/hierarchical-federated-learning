# edge_server/utils/model_utils.py

import torch

def average_state_dicts(state_dicts):
    """
    複数の state_dict を平均化して返す。
    引数:
        state_dicts (List[Dict[str, torch.Tensor]]): 各端末からの重み
    戻り値:
        Dict[str, torch.Tensor]: 平均化された state_dict
    """
    if not state_dicts:
        raise ValueError("Empty list of state_dicts")

    avg_state_dict = {}
    num_dicts = len(state_dicts)

    # 最初の state_dict の key を使って初期化
    for key in state_dicts[0].keys():
        # 各 key の tensor を合計する
        stacked = torch.stack([sd[key] for sd in state_dicts])
        avg_state_dict[key] = torch.mean(stacked, dim=0)

    return avg_state_dict

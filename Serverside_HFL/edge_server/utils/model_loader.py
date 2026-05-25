from pathlib import Path
from typing import Any, Optional, Union
import torch
import io

def load_torch_artifact(
    path: Union[str, Path], map_location: Union[str, torch.device] = "cpu"
) -> Any:
    p = Path(path)
    # try load as TorchScript via file-like
    try:
        with open(p, "rb") as f:
            return torch.jit.load(f, map_location=map_location)
    except Exception:
        pass

    # try torch.load
    try:
        obj = torch.load(str(p), map_location=map_location)
    except Exception as e:
        raise RuntimeError(f"モデル成果物の読み込みに失敗しました {p}: {e}") from e

    # if it's a state_dict, return the dict so caller can construct
    if isinstance(obj, dict):
        return obj

    # assume it's a full model
    return obj

def build_model_from_state_dict(state_dict: dict, model_ctor) -> torch.nn.Module:
    # Try to infer model dimensions from common layer names in the state_dict
    try:
        # layer1.weight: (hidden, input)
        l1w = state_dict.get("layer1.weight")
        l3w = state_dict.get("layer3.weight") or state_dict.get("layer2.weight")
        if l1w is not None:
            input_size = int(l1w.shape[1])
            hidden_size = int(l1w.shape[0])
        else:
            input_size = None
            hidden_size = None
        if l3w is not None:
            output_size = int(l3w.shape[0])
        else:
            output_size = None
    except Exception:
        input_size = hidden_size = output_size = None

    # If we inferred sizes, try to pass them to model_ctor if it accepts params
    model = None
    if input_size and hidden_size and output_size:
        try:
            model = model_ctor(model_params=[input_size, hidden_size, output_size])
        except TypeError:
            try:
                model = model_ctor([input_size, hidden_size, output_size])
            except Exception:
                model = None

    if model is None:
        # Fallback to calling without args
        model = model_ctor()

    missing, unexpected = model.load_state_dict(state_dict, strict=False)
    model.eval()
    return model

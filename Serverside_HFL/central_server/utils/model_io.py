# utils/model_io.py（新規 or 既存のユーティリティに追加）
from pathlib import Path
import torch
from typing import Any, Callable, Optional, Union

def load_torch_artifact(
    path: Union[str, Path],
    model_ctor: Optional[Callable[[], torch.nn.Module]] = None,
    map_location: Union[str, torch.device] = "cpu",
) -> Any:
    p = Path(path)

    # まず TorchScript を file-like 経由で試す（パス文字コード問題の回避にも有効）
    try:
        with open(p, "rb") as f:
            return torch.jit.load(f, map_location=map_location)
    except Exception as e_jit:
        # state_dict / full model を試す
        try:
            obj = torch.load(p, map_location=map_location)
        except Exception as e_load:
            raise RuntimeError(
                f"TorchScript でも torch.load でも読み込めませんでした (jit={e_jit}) (load={e_load}) path={p}"
            )

        # state_dict なら model_ctor が必要
        if isinstance(obj, dict):
            if model_ctor is None:
                raise RuntimeError(
                    "state_dict を読み込みましたが model_ctor が None です。"
                    "モデルを再構築するコンストラクタを渡してください。"
                )
            model = model_ctor()
            missing, unexpected = model.load_state_dict(obj, strict=False)
            model.eval()
            return {"model": model, "missing_keys": missing, "unexpected_keys": unexpected}
        else:
            # まるごと保存されたモデル（torch.save(model)）の可能性
            try:
                obj.eval()
            except Exception:
                pass
            return obj

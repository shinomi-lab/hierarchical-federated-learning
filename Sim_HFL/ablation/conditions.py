"""条件の生成: 基準 yaml + 3軸のうち1軸だけを変えた条件群（sim-factor-ablation）。"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

BASE_YAML = Path(__file__).resolve().parent.parent / "config" / "baseline_hard_async.yaml"
OUT_DIR = Path(__file__).resolve().parent.parent / "config" / "ablation"

# 軸 → (水準名, 上書き内容)。上書きは (キーのパス, 値) のリスト。
# AP 混雑は Async / Sync の両ランナーが読む AP-A のチャネル数で表す（background_load は Sync のみ）。
AXES: dict[str, list[tuple[str, list[tuple[tuple[str, ...], object]]]]] = {
    "speed": [
        ("0", [(("async", "terminal_speed_std"), 0.0)]),
        ("0.5", [(("async", "terminal_speed_std"), 0.5)]),
        ("1.0", [(("async", "terminal_speed_std"), 1.0)]),
    ],
    "data": [
        ("dir0.5", [(("data", "iid"), False), (("data", "dirichlet_alpha"), 0.5)]),
        ("dir0.1", [(("data", "iid"), False), (("data", "dirichlet_alpha"), 0.1)]),
    ],
    # チャネル数は Erlang-B のパケット損失(スループット側)にしか効かず、満足度(RTT)に届かなかった（design.md 実装で判明したこと 5）。結果に残すが結論には使わない。
    "ap": [
        ("ch1", [(("data", "ap_a", "n_channels"), 1)]),
        ("ch4", [(("data", "ap_a", "n_channels"), 4)]),
    ],
    # RTT を決める AP-A のサービス率。小さいほど同じ接続数で混む。
    # ※ AP-A にはスループット重視の端末(ブラウザ・動画)しか乗らず、要求が常に満たされるため満足度に届かなかった（design.md の 6）。
    "apmu": [
        ("mu1", [(("data", "ap_a", "mu_rtt"), 1.0)]),
        ("mu4", [(("data", "ap_a", "mu_rtt"), 4.0)]),
    ],
    # AP-B のサービス率。遅延重視の端末(通話・配信)が乗る側なので、満足度を決める。基準 5.0。
    "apmuB": [
        ("mu2.5", [(("data", "ap_b", "mu_rtt"), 2.5)]),
        ("mu10", [(("data", "ap_b", "mu_rtt"), 10.0)]),
    ],
}


def load_base(path: Path = BASE_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # baseline_hard_async.yaml の output_size: 4 は SP3a 以前の値。現行の初期モデル・実機は AP 数 (2)。
    # 全条件に共通の正規化なので、軸ではない（既存 yaml は変更しない）。
    cfg["model"]["output_size"] = 2
    return cfg


def _set(cfg: dict, keys: tuple[str, ...], value: object) -> None:
    node = cfg
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value


def _flatten(cfg: dict, prefix: tuple[str, ...] = ()) -> dict:
    out = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            out.update(_flatten(v, prefix + (k,)))
        else:
            out[prefix + (k,)] = v
    return out


def build_conditions(base: dict | None = None) -> list[dict]:
    """[{name, axis, level, cfg}] を返す。先頭は基準条件 (axis=None)。"""
    base = base if base is not None else load_base()
    conds = [{"name": "base", "axis": None, "level": None, "cfg": copy.deepcopy(base)}]
    for axis, levels in AXES.items():
        for level, overrides in levels:
            cfg = copy.deepcopy(base)
            for keys, value in overrides:
                _set(cfg, keys, value)
            conds.append({"name": f"{axis}_{level}", "axis": axis, "level": level, "cfg": cfg})
    return conds


def differing_keys(a: dict, b: dict) -> set[tuple[str, ...]]:
    fa, fb = _flatten(a), _flatten(b)
    return {k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k)}


def write_yamls(out_dir: Path = OUT_DIR, base: dict | None = None) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    conds = build_conditions(base)
    for c in conds:
        with open(out_dir / f"{c['name']}.yaml", "w", encoding="utf-8") as f:
            f.write("# 自動生成 (ablation/conditions.py)。手で編集しない。\n")
            yaml.safe_dump(c["cfg"], f, allow_unicode=True, sort_keys=False)
    return conds


if __name__ == "__main__":
    for c in write_yamls():
        print(c["name"], c["axis"], c["level"])

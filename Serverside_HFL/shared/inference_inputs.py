from pathlib import Path


def load_norm_stats(path) -> dict:
    """推論の正規化統計（tools/make_norm_stats.py の出力）。無ければ正規化しない（mean=0,std=1）。"""
    import json
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"tp_mean": 0.0, "tp_std": 1.0, "rtt_mean": 0.0, "rtt_std": 1.0}


def inference_input(record: dict, apps: list, norm: dict):
    """DB 行 → 推論入力。先行研究どおり、学習と同じ「アプリの要求値（z-score）＋one-hot」。実測 TP/RTT は使わない。"""
    names = [a.get("appType") for a in apps]
    at = record.get("app_type")
    if at not in names:
        return None
    i = names.index(at)
    need_tp = float(apps[i].get("needTP", 0)); need_rtt = float(apps[i].get("needRTT", 0))
    x0 = (need_tp - norm["tp_mean"]) / (norm["tp_std"] or 1.0)
    x1 = (need_rtt - norm["rtt_mean"]) / (norm["rtt_std"] or 1.0)
    one_hot = [1.0 if j == i else 0.0 for j in range(max(4, len(apps)))]
    return [x0, x1] + one_hot[:4]

#!/usr/bin/env python3
"""
compare_ablation_n5.py
======================
n=5 ablation 結果を集計し、条件間の mean±std と簡易 t 検定を出力する。

使い方:
  python3 analysis/compare_ablation_n5.py \
      --runs-list logs/ablation_n5_20260616/A_current.log \
                  logs/ablation_n5_20260616/B_no_blend.log \
                  logs/ablation_n5_20260616/C_no_blend_term_cont.log

run_sim.py --runs N の出力ログから run_id と最終 val_acc を抽出し、
results/logs/rounds_*.jsonl から各 run の詳細指標 (HM, AP 切替) を集計する。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from pathlib import Path


def _extract_run_ids(log_path: Path) -> list[str]:
    """run_sim.py の標準出力ログから run_id を順に抽出。"""
    ids: list[str] = []
    if not log_path.exists():
        return ids
    for line in open(log_path, encoding="utf-8", errors="replace"):
        m = re.search(r"run_id\s*:\s*([0-9a-f]{8})", line)
        if m:
            ids.append(m.group(1))
    # 重複排除しつつ順序保持
    seen = set()
    ordered = []
    for r in ids:
        if r not in seen:
            seen.add(r)
            ordered.append(r)
    return ordered


def _find_jsonl_for_run(results_dir: Path, run_id_short: str) -> Path | None:
    for p in sorted(results_dir.glob(f"rounds_*_{run_id_short}.jsonl")):
        return p
    return None


def _summarize_run(jsonl_path: Path) -> dict:
    """1 つの rounds_*.jsonl から指標を集計。"""
    hm_total = 0.0
    hm_count = 0
    ap_switches = 0
    val_accs: list[float] = []
    val_accs_cycle1: list[float] = []  # composite_id=1 の val_acc (G=1, L=1)
    for line in open(jsonl_path, encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        ev = e.get("event", "")
        if ev == "satisfaction_summary":
            hm = e.get("harmonic_mean_satisfaction")
            if hm is not None:
                hm_total += hm
                hm_count += 1
        elif ev == "inference_result":
            if e.get("switched"):
                ap_switches += 1
        elif ev == "aggregation_complete":
            va = e.get("avg_val_acc")
            if va is not None:
                val_accs.append(va)
                # composite_id=1 が最初の cycle
                if e.get("round_id") == 1 and len(val_accs_cycle1) == 0:
                    val_accs_cycle1.append(va)
    return {
        "val_acc_final": val_accs[-1] if val_accs else None,
        "val_acc_max":   max(val_accs) if val_accs else None,
        "val_acc_cycle1": val_accs_cycle1[0] if val_accs_cycle1 else None,
        "hm_mean":       hm_total / hm_count if hm_count else None,
        "ap_switches":   ap_switches,
    }


def _stat(label: str, vals: list[float], fmt: str = ".4f") -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return f"{label}: —"
    m = statistics.mean(vals)
    s = statistics.stdev(vals) if len(vals) >= 2 else 0.0
    return f"{label}: {m:{fmt}} ± {s:{fmt}} (n={len(vals)})"


def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 500, 3e-15, 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > FPMIN else FPMIN)
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = d if abs(d) > FPMIN else FPMIN
        c = 1 + aa / c; c = c if abs(c) > FPMIN else FPMIN
        d = 1 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = d if abs(d) > FPMIN else FPMIN
        c = 1 + aa / c; c = c if abs(c) > FPMIN else FPMIN
        d = 1 / d; de = d * c; h *= de
        if abs(de - 1) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """正則化不完全ベータ関数 I_x(a, b)。"""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    return bt * _betacf(a, b, x) / a if x < (a + 1) / (a + b + 2) else 1 - bt * _betacf(b, a, 1 - x) / b


def _t_p_two_sided(t: float, df: float) -> float:
    """t 分布の両側 p（scipy なし）。t・df が NaN（検定が退化）なら NaN。"""
    if math.isnan(t) or math.isnan(df):
        return float("nan")
    if math.isinf(t):
        return 0.0
    return _betai(df / 2, 0.5, df / (df + t * t))


def _welch_t_test(a: list[float], b: list[float]) -> tuple[float, float]:
    """Welch's t-test (unequal var). 戻り値 (t, df 近似)。p は _t_p_two_sided で出す。"""
    a = [x for x in a if x is not None]
    b = [x for x in b if x is not None]
    if len(a) < 2 or len(b) < 2:
        return (float("nan"), float("nan"))
    ma, mb = statistics.mean(a), statistics.mean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    na, nb = len(a), len(b)
    if va + vb == 0:
        return (float("nan"), float("nan"))
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return (float("nan"), float("nan"))
    t = (ma - mb) / se
    df_num = (va / na + vb / nb) ** 2
    df_den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = df_num / df_den if df_den else float("nan")
    return (t, df)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--logs", nargs="+", required=True,
                   help="run_sim.py の標準出力ログを条件ごとに指定 (例: A.log B.log C.log)")
    p.add_argument("--labels", nargs="+", default=None,
                   help="各条件のラベル (省略時はファイル名)")
    p.add_argument("--results-dir", default="results/logs",
                   help="rounds_*.jsonl が置かれているディレクトリ")
    p.add_argument("--output", default=None, help="Markdown 出力先 (省略時 stdout)")
    args = p.parse_args()

    labels = args.labels or [Path(p).stem for p in args.logs]
    results_dir = Path(args.results_dir)

    # 各条件の集計
    per_cond: dict[str, list[dict]] = {}
    per_cond_run_ids: dict[str, list[str]] = {}
    for label, log in zip(labels, args.logs):
        run_ids = _extract_run_ids(Path(log))
        per_cond_run_ids[label] = run_ids
        rows: list[dict] = []
        for rid in run_ids:
            jp = _find_jsonl_for_run(results_dir, rid)
            if jp is None:
                print(f"[警告] jsonl が見つかりません: run_id={rid}")
                continue
            rows.append(_summarize_run(jp))
        per_cond[label] = rows

    # 集計出力
    lines: list[str] = []
    lines.append("# Ablation n=5 集計\n")
    lines.append(f"results dir: `{results_dir}`")
    lines.append("")
    lines.append("## 条件別サマリー (mean ± std, n=試行数)")
    lines.append("")
    lines.append("| 条件 | val_acc final | val_acc cycle1 | HM mean | AP switches |")
    lines.append("|------|---------------|----------------|---------|-------------|")
    for label in labels:
        rows = per_cond.get(label, [])
        vf = [r["val_acc_final"] for r in rows]
        vc1 = [r["val_acc_cycle1"] for r in rows]
        hm = [r["hm_mean"] for r in rows]
        ap = [r["ap_switches"] for r in rows]
        def _fmt(vals, pct=False, fmt=".4f"):
            vals = [v for v in vals if v is not None]
            if not vals:
                return "—"
            m = statistics.mean(vals)
            s = statistics.stdev(vals) if len(vals) >= 2 else 0.0
            if pct:
                return f"{m*100:.2f}% ± {s*100:.2f}%"
            return f"{m:{fmt}} ± {s:{fmt}}"
        lines.append(
            f"| {label} | {_fmt(vf, pct=True)} | {_fmt(vc1, pct=True)} | "
            f"{_fmt(hm, pct=True)} | {_fmt(ap, fmt='.2f')} |"
        )
    lines.append("")

    # 詳細表
    for label in labels:
        rows = per_cond.get(label, [])
        rids = per_cond_run_ids.get(label, [])
        lines.append(f"### {label} (n={len(rows)})")
        lines.append("")
        lines.append("| seed_idx | run_id | val_acc final | val_acc cycle1 | HM | AP sw |")
        lines.append("|---|---|---|---|---|---|")
        for i, (rid, r) in enumerate(zip(rids, rows)):
            vf = f"{r['val_acc_final']*100:.2f}%" if r["val_acc_final"] is not None else "—"
            vc1 = f"{r['val_acc_cycle1']*100:.2f}%" if r["val_acc_cycle1"] is not None else "—"
            hm = f"{r['hm_mean']*100:.2f}%" if r["hm_mean"] is not None else "—"
            lines.append(f"| {i} | {rid} | {vf} | {vc1} | {hm} | {r['ap_switches']} |")
        lines.append("")

    # 簡易 t 検定 (隣接ペアと A vs C)
    if len(labels) >= 2:
        lines.append("## Welch t 検定 (隣接ペア + 端ペア)")
        lines.append("")
        lines.append("| 指標 | ペア | mean diff | t | df ≈ | p（両側） |")
        lines.append("|------|------|-----------|---|------|-----------|")

        def _diff_and_t(metric: str, a_rows, b_rows):
            a_vals = [r[metric] for r in a_rows]
            b_vals = [r[metric] for r in b_rows]
            a_clean = [v for v in a_vals if v is not None]
            b_clean = [v for v in b_vals if v is not None]
            if not a_clean or not b_clean:
                return None
            mean_diff = statistics.mean(a_clean) - statistics.mean(b_clean)
            t, df = _welch_t_test(a_vals, b_vals)
            return mean_diff, t, df

        pairs: list[tuple[str, str]] = []
        for i in range(len(labels) - 1):
            pairs.append((labels[i], labels[i + 1]))
        if len(labels) >= 3:
            pairs.append((labels[0], labels[-1]))
        for metric in ["val_acc_cycle1", "val_acc_final", "hm_mean", "ap_switches"]:
            for a, b in pairs:
                r = _diff_and_t(metric, per_cond[a], per_cond[b])
                if r is None:
                    continue
                md, t, df = r
                pv = _t_p_two_sided(t, df)
                lines.append(
                    f"| {metric} | {a} vs {b} | {md:+.4f} | "
                    f"{('NaN' if math.isnan(t) else f'{t:.3f}')} | "
                    f"{('NaN' if math.isnan(df) else f'{df:.2f}')} | "
                    f"{('検定不可' if math.isnan(pv) else f'{pv:.3f}')} |"
                )
        lines.append("")
        lines.append("注: p は Welch の t を t 分布（df は Welch–Satterthwaite）で両側に換算した値。"
                     "「検定不可」は両群の分散が 0 で t が定義できない（同じ seed で同じ値になった等）。"
                     "同じ seed 列で全条件が同値になる指標（t=0・p=1）は差が無い証拠ではない。")
        lines.append("")

    out = "\n".join(lines)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"Written: {args.output}")
    else:
        print(out)


if __name__ == "__main__":
    main()

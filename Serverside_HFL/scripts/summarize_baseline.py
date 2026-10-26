#!/usr/bin/env python3
"""ベースライン（baseline_session）の結果を端末ごとの表にする。

  Serverside_HFL/.venv/bin/python3 Serverside_HFL/scripts/summarize_baseline.py --db <training_metrics.db> --after-id N [--csv out.csv]

N より後（id > N）の baseline_session 行を対象にする。列: 端末・アプリ・モード・開始/After 時点の AP・Before の平均・After の平均・
差・After 切替の確認・Greedy の出所・推奨 AP（hfl_rec／central）・推奨に従ったか・初期モデルか。After は後から補完されるため、無い行は '-'。
hfl_rec で初期モデル（集約前、hfl_rec_is_initial が真）のセッションは、既定で集計から除く（--include-initial で含める）。
末尾に方式ごとの要約（n・After−Before の平均・従った n／全 n・除外した初期モデル n）と、hfl_rec と central の推奨 AP の一致を出す。標準ライブラリのみ。
"""
import argparse
import csv
import json
import sqlite3
import sys


def rows(db, after_id):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute("select id, terminal_id, raw_json from metrics where id > ? order by id", (after_id,)):
        try:
            j = json.loads(r["raw_json"] or "{}")
        except Exception:
            continue
        if j.get("row_kind") != "baseline_session":
            continue
        before, after = j.get("satisfaction_baseline_mean"), j.get("satisfaction_after")
        out.append({
            "id": r["id"], "terminal": r["terminal_id"], "app": j.get("app_type"), "mode": j.get("baseline_mode"),
            "ap_start": j.get("connected_ap"), "ap_after_start": j.get("connected_ap_after_start"), "ap_after_end": j.get("connected_ap_after_end"),
            "before": before, "after": after, "after_n": j.get("satisfaction_after_n"),
            "delta": (after - before) if isinstance(before, (int, float)) and isinstance(after, (int, float)) else None,
            "switch": j.get("after_switch_confirmed"), "sources": j.get("greedy_ap_sources"),
            # hfl-recommended-ap-mode: 推奨 AP（hfl_rec／central）・推奨に従ったか・初期モデルか
            "rec_ap": j.get("hfl_rec_ap") if j.get("baseline_mode") == "hfl_rec" else j.get("central_ap"),
            "followed": j.get("hfl_rec_followed"), "initial": j.get("hfl_rec_is_initial"),
            # fix-audit-20261026: Random のセッションで選んだ AP・After 前の安定待ち（以前のデータは無し＝条件差）
            "random_ap": j.get("random_choice_ap"), "settle_ms": j.get("after_settle_ms"),
        })
    return out


def split_initial(rs, include_initial=False):
    """初期モデル（集約前）で問い合わせたセッションを除く。戻り値は (残した行, 初期モデルだった行数)。"""
    n_init = sum(1 for r in rs if r["initial"] is True)
    return (rs if include_initial else [r for r in rs if r["initial"] is not True]), n_init


def mode_summary(rs, initial_by_mode=None):
    """方式ごとの n・After−Before の平均・従った n／全 n（hfl_rec のみ。不明 null は分母に入れない）・除外した初期モデル n。"""
    initial_by_mode = initial_by_mode or {}
    out = []
    for m in sorted({r["mode"] for r in rs}, key=lambda x: (x is None, str(x))):
        g = [r for r in rs if r["mode"] == m]
        ds = [r["delta"] for r in g if r["delta"] is not None]
        known = [r["followed"] for r in g if r["followed"] is not None]
        out.append({"mode": m, "n": len(g), "delta": (sum(ds) / len(ds)) if ds else None,
                    "followed_n": sum(1 for f in known if f) if m == "hfl_rec" else None,
                    "followed_total": len(known) if m == "hfl_rec" else None,
                    "initial_n": initial_by_mode.get(m, 0)})
    return out


def rec_agreement(rs):
    """hfl_rec と central の推奨 AP が、端末ごと（その端末の最後の推奨）に一致した数と、比べられた端末数。"""
    last = {"hfl_rec": {}, "central": {}}
    for r in rs:
        if r["mode"] in last and r["rec_ap"] is not None:
            last[r["mode"]][r["terminal"]] = r["rec_ap"]
    both = set(last["hfl_rec"]) & set(last["central"])
    return sum(1 for t in both if last["hfl_rec"][t] == last["central"][t]), len(both)


def fmt(v, nd=3):
    if v is None:
        return "-"
    return ("%.*f" % (nd, v)) if isinstance(v, float) else str(v)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--after-id", type=int, default=0)
    p.add_argument("--csv")
    p.add_argument("--include-initial", action="store_true", help="hfl_rec の初期モデル（集約前）のセッションも集計に含める")
    a = p.parse_args(argv)
    rs, n_init = split_initial(rows(a.db, a.after_id), a.include_initial)
    cols = ["terminal", "app", "mode", "ap_start", "ap_after_start", "ap_after_end", "before", "after", "after_n", "delta", "switch", "sources",
            "rec_ap", "followed", "initial", "random_ap", "settle_ms"]
    print("  ".join("%-14s" % c if c in ("terminal", "app") else "%s" % c for c in cols))
    for r in rs:
        print("  ".join("%-14s" % fmt(r[c]) if c in ("terminal", "app") else fmt(r[c]) for c in cols))
    if rs:
        print()
        for m in mode_summary(rs, {"hfl_rec": 0 if a.include_initial else n_init}):
            fol = "" if m["followed_n"] is None else "  従った %d/%d" % (m["followed_n"], m["followed_total"])
            ini = "  初期モデル除外 %d" % m["initial_n"] if m["initial_n"] else ""
            print("%-8s n=%d  After−Before=%s%s%s" % (m["mode"], m["n"], fmt(m["delta"]), fol, ini))
        agree, total = rec_agreement(rs)
        if total:
            print("hfl_rec と central の推奨 AP が一致: %d/%d 端末" % (agree, total))
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["id"] + cols)
            w.writeheader()
            for r in rs:
                w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return 0 if rs else 1


if __name__ == "__main__":
    sys.exit(main())

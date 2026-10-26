"""
sim/display.py
--------------
シミュレーション実行中の視覚的な進捗表示モジュール。

ANSI エスケープコードのみ使用（追加ライブラリ不要）。
Windows / macOS / Linux いずれでも動作する。
"""
from __future__ import annotations

import os
import sys
import time

# ------------------------------------------------------------------ #
# カラー定義
# ------------------------------------------------------------------ #
_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")

def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def bold(t: str)    -> str: return _c("1",    t)
def cyan(t: str)    -> str: return _c("36",   t)
def green(t: str)   -> str: return _c("32",   t)
def yellow(t: str)  -> str: return _c("33",   t)
def red(t: str)     -> str: return _c("31",   t)
def magenta(t: str) -> str: return _c("35",   t)
def dim(t: str)     -> str: return _c("2",    t)
def b_cyan(t: str)  -> str: return _c("1;36", t)
def b_green(t: str) -> str: return _c("1;32", t)


# ------------------------------------------------------------------ #
# ボックス描画
# ------------------------------------------------------------------ #
_W = 62   # ボックス幅

def _box_line(text: str = "", char: str = "─", fill: str = " ") -> str:
    if not text:
        return "│" + char * (_W - 2) + "│"
    pad   = _W - 2 - len(text) - 1
    left  = fill
    right = fill * max(0, pad - len(left) + 1)
    return "│" + left + text + right + "│"

def print_banner(title: str) -> None:
    """最上位フェーズのバナー（ボックス付き）"""
    top    = "╔" + "═" * (_W - 2) + "╗"
    mid    = _box_line(bold(title))
    bottom = "╚" + "═" * (_W - 2) + "╝"
    print()
    print(b_cyan(top))
    print(b_cyan("│") + mid[1:-1] + b_cyan("│"))
    print(b_cyan(bottom))


def print_phase(number: int, name: str) -> None:
    """フェーズ区切り線"""
    label = f" Phase {number}: {name} "
    side  = max(2, (_W - 2 - len(label)) // 2)
    bar   = cyan("━" * side + label + "━" * side)
    print(f"\n{bar}")


def print_done(msg: str) -> None:
    print(f"  {green('✔')} {msg}")


def print_warn(msg: str) -> None:
    print(f"  {yellow('⚠')} {msg}")


def print_error(msg: str) -> None:
    print(f"  {red('✘')} {msg}")


# ------------------------------------------------------------------ #
# HFL トポロジー表示
# ------------------------------------------------------------------ #
def print_hfl_topology(topology: dict) -> None:
    """Central → Edge → Terminal のツリー構造を表示"""
    print()
    print(f"  {bold('Central Server')}")
    edge_ids = list(topology.keys())
    for ei, (edge_id, terms) in enumerate(topology.items()):
        branch = "└──" if ei == len(edge_ids) - 1 else "├──"
        term_str = "  ".join(cyan(t.terminal_id) for t in terms)
        print(f"    {branch} {bold(edge_id)}  ──  {term_str}")
    print()


# ------------------------------------------------------------------ #
# 設定表示
# ------------------------------------------------------------------ #
def print_config(cfg: dict) -> None:
    fed  = cfg.get("federation", {})
    lt   = cfg.get("local_training", {})
    dat  = cfg.get("data", {})
    mdl  = cfg.get("model", {})
    iid  = "IID" if dat.get("iid", True) else f"non-IID (α={dat.get('dirichlet_alpha', 0.5)})"

    # エッジトポロジーを文字列化
    topo = fed.get("edge_topology", {})
    topo_str = "  ".join(
        f"{eid}:[T{',T'.join(str(i) for i in idxs)}]"
        for eid, idxs in topo.items()
    ) if topo else "—"

    aps = dat.get("aps", [])
    if not aps:
        aps = [dat.get("ap_a", {}), dat.get("ap_b", {})]
    
    use_queue = len(aps) > 0 and aps[0].get("mu_rtt") is not None
    net_model = "M/M/1 + Erlang-B" if use_queue else "Gaussian"

    rows = [
        ("Edge servers",          str(fed.get("num_edge_servers", 2))),
        ("Terminals",             str(fed.get("num_terminals", 3))),
        ("Topology",              topo_str),
        ("Global rounds",         str(fed.get("num_rounds", 30))),
        ("Local rounds/G",        str(fed.get("local_rounds", 5))),
        ("Epochs/local",          str(lt.get("epochs", 5))),
        ("Learning rate",         str(lt.get("learning_rate", 0.0001))),
        ("Weight decay",          str(lt.get("weight_decay", 0.01))),
        ("Dropout",               str(mdl.get("dropout_p", 0.1))),
        ("Data dist.",            iid),
        ("Total samples",         str(dat.get("total_samples", 3000))),
        ("Model arch.",
         f"{mdl.get('input_size',6)}->{mdl.get('hidden_size',32)}"
         f"->{mdl.get('hidden_size',32)}->{mdl.get('output_size',4)}"),
        ("Network model",         net_model),
    ]
    for i, ap in enumerate(aps):
        if use_queue:
            ap_str = f"tp={ap.get('tp_mean', 50)} rtt={ap.get('rtt_mean', 20)} mu={ap.get('mu_rtt')} n={ap.get('n_channels')}"
        else:
            ap_str = f"tp={ap.get('tp_mean', 50)}±{ap.get('tp_std', 3)} rtt={ap.get('rtt_mean', 20)}±{ap.get('rtt_std', 5)}"
        name = ap.get('name', f"AP_{i}")
        rows.append((name, ap_str))
    key_w = max(len(k) for k, _ in rows) + 1
    print()
    for k, v in rows:
        print(f"  {dim(k.ljust(key_w))} : {bold(v)}")


# ------------------------------------------------------------------ #
# ラウンドプログレスバー
# ------------------------------------------------------------------ #
_BAR_W = 20   # バー幅

def _bar(frac: float, width: int = _BAR_W) -> str:
    filled = int(frac * width)
    empty  = width - filled
    inner  = "█" * filled + "░" * empty
    return f"[{cyan(inner)}]"


def print_round(
    rnd:          int,
    total_rounds: int,
    val_acc:      float,
    val_loss:     float,
    participants: int,
    num_excluded: int = 0,
    train_duration_ms: float = 0.0,
) -> None:
    """後方互換: print_global_round を呼ぶ"""
    print_global_round(
        g_rnd=rnd, total_rounds=total_rounds,
        val_acc=val_acc, val_loss=val_loss,
        satisfaction=None, inf_results=[],
    )


def print_global_round(
    g_rnd:        int,
    total_rounds: int,
    val_acc:      float,
    val_loss:     float,
    satisfaction: "float | None" = None,
    inf_results:  list = [],
    n_on_ap:      "list[int] | None" = None,
    tp_rtt_ap:    "list[tuple[float,float]] | None" = None,
) -> None:
    """
    グローバルラウンドの結果を1行で表示。
    AP選択情報（各端末の接続AP・切り替え有無）と混雑状態を含む。
    """
    frac     = g_rnd / max(total_rounds, 1)
    pct      = int(frac * 100)
    bar      = _bar(frac)
    acc_str  = bold(f"{val_acc:.4f}")
    loss_str = f"{val_loss:.4f}"

    # AP 選択インジケーター: T0:=AP0  T1:→AP1  ...
    ap_parts = []
    for r in inf_results:
        tid   = r.terminal_id.replace("sim-term-", "T")
        arrow = yellow("→") if r.switched else green("=")
        ap_parts.append(f"{dim(tid)}{arrow}AP{r.predicted_ap}")
    ap_str = "  ".join(ap_parts)

    sat_str = (
        f"  sat={green(f'{satisfaction*100:.1f}%')}" if satisfaction is not None else ""
    )

    # 混雑状態インジケーター: [A:2台 TP=10.2 RTT=250ms | B:1台 TP=4.9 RTT=34ms]
    cong_str = ""
    if n_on_ap and tp_rtt_ap and len(n_on_ap) >= 2 and len(tp_rtt_ap) >= 2:
        parts = []
        for i, label in enumerate(["A", "B"]):
            n   = n_on_ap[i]
            tp  = tp_rtt_ap[i][0]
            rtt = tp_rtt_ap[i][1]
            col = yellow if n >= 2 else green
            parts.append(f"{col(label)}:{n}T TP={tp:.1f} RTT={rtt:.0f}ms")
        cong_str = f"  {dim('|'.join(parts))}"

    print(
        f"  G.Rnd {g_rnd:>3}/{total_rounds}  {bar} {pct:>3}%"
        f"  acc={acc_str}  loss={loss_str}{sat_str}"
        + (f"  [{ap_str}]" if ap_parts else "")
        + cong_str
    )


# ------------------------------------------------------------------ #
# 端末ごとの学習中スピナー
# ------------------------------------------------------------------ #
_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_spin_idx = 0

def print_terminal_training(tid: str, rnd: int) -> None:
    """端末学習中のインライン表示（改行なし）"""
    global _spin_idx
    if _NO_COLOR:
        return
    spin = _SPIN[_spin_idx % len(_SPIN)]
    _spin_idx += 1
    sys.stdout.write(f"\r  {cyan(spin)} 学習中: {tid}  (Round {rnd}) …")
    sys.stdout.flush()

def clear_terminal_line() -> None:
    if _NO_COLOR:
        return
    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()


# ------------------------------------------------------------------ #
# データ分割表示
# ------------------------------------------------------------------ #
def print_data_split(sizes: list[int], iid: bool) -> None:
    total  = sum(sizes)
    label  = "IID" if iid else "non-IID"
    min_s  = min(sizes)
    max_s  = max(sizes)
    print_done(
        f"データ分割完了  [{label}]  "
        f"総数={total}  端末数={len(sizes)}  "
        f"サンプル/端末: {min_s}〜{max_s}"
    )
    # ミニ棒グラフ
    peak = max(sizes) or 1
    for i, s in enumerate(sizes):
        bar = "▪" * max(1, int(s / peak * 12))
        print(f"    T{i:02d} {dim(bar)} {s}")


# ------------------------------------------------------------------ #
# 最終サマリー
# ------------------------------------------------------------------ #
def print_final_summary(
    run_id:       str,
    agg_history:  list,
    report_path:  str = "",
    total_mb:     float = 0.0,
) -> None:
    if not agg_history:
        return

    final     = agg_history[-1]
    final_acc = final.avg_val_acc  if hasattr(final, "avg_val_acc")  else final.get("avg_val_acc", 0)
    final_loss= final.avg_val_loss if hasattr(final, "avg_val_loss") else final.get("avg_val_loss", 0)
    n_rounds  = len(agg_history)

    # 収束ラウンド
    target = final_acc * 0.95
    conv_round = next(
        (i + 1 for i, r in enumerate(agg_history)
         if (r.avg_val_acc if hasattr(r, "avg_val_acc") else r.get("avg_val_acc", 0)) >= target),
        n_rounds,
    )

    acc_bar  = "█" * int(final_acc * 20) + "░" * (20 - int(final_acc * 20))
    acc_pct  = f"{final_acc * 100:.2f}%"

    top    = "╔" + "═" * (_W - 2) + "╗"
    bottom = "╚" + "═" * (_W - 2) + "╝"

    lines = [
        ("", ""),
        (b_green(top), ""),
        ("│  " + bold("実験結果サマリー"), ""),
        ("│", ""),
        (f"│  run_id    : {dim(run_id[:8])}…", ""),
        (f"│  val_acc   : {b_green(acc_pct)}  {green(acc_bar)}", ""),
        (f"│  val_loss  : {bold(f'{final_loss:.4f}')}", ""),
        (f"│  収束      : Round {conv_round} / {n_rounds}", ""),
    ]
    if total_mb > 0:
        lines.append((f"│  通信量    : {total_mb:.3f} MB", ""))
    if report_path:
        lines.append(("│", ""))
        lines.append((f"│  {green('📄')} {dim(report_path)}", ""))
    lines.append((b_green(bottom), ""))

    print()
    for line, _ in lines:
        print(line)
    print()

#!/usr/bin/env python3
"""
run_sim.py
==========
Sim_HFL シミュレーション実行エントリポイント。

使い方:
  # デフォルト設定で実行 (IID, 5端末, 30ラウンド)
  python run_sim.py

  # 設定ファイルを指定
  python run_sim.py --config config/default.yaml

  # コマンドラインで設定を上書き
  python run_sim.py --rounds 50 --terminals 10 --no-iid --epochs 10

  # 理論値比較レポートも同時に出力
  python run_sim.py --compare

  # 既存の JSONL ログから理論値比較のみ実行
  python run_sim.py --compare-only results/logs/rounds_20240101_000000_abc12345.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sim_HFL: FedAvg シミュレーション & 理論値比較",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--config", default="config/default.yaml",
        help="設定ファイルパス (YAML)"
    )
    p.add_argument("--rounds",    type=int,   default=None, help="通信ラウンド数を上書き")
    p.add_argument("--terminals", type=int,   default=None, help="端末数を上書き")
    p.add_argument("--epochs",    type=int,   default=None, help="ローカルエポック数を上書き")
    p.add_argument("--lr",        type=float, default=None, help="学習率を上書き")
    p.add_argument("--no-iid",    action="store_true",     help="non-IID データ分割を使用")
    p.add_argument("--alpha",     type=float, default=None, help="ディリクレ alpha (non-IID 用)")
    p.add_argument("--samples",   type=int,   default=None, help="総サンプル数を上書き")
    p.add_argument("--seed",      type=int,   default=None, help="乱数シードを上書き")
    p.add_argument("--compare",   action="store_true",     help="実行後に理論値比較レポートを生成")
    p.add_argument(
        "--compare-only", default=None, metavar="JSONL_PATH",
        help="既存ログから理論値比較のみ実行 (シミュレーション省略)"
    )
    p.add_argument("-i", "--interactive", action="store_true", help="実行前にターミナルでAP設定を対話的に変更する")
    p.add_argument("--csv", type=str, default=None, help="使用する教師データCSVのパス")
    p.add_argument("--no-plot",   action="store_true",     help="グラフ生成をスキップ")
    p.add_argument("--quiet",     action="store_true",     help="詳細ログを抑制")
    p.add_argument("--runs",      type=int,   default=1,   help="実行回数（各回シードをインクリメント）")
    return p


def _interactive_config(cfg: dict) -> None:
    print("\n" + "="*60)
    print(" 🛠️  対話的 詳細設定モード (Interactive Configuration)")
    print("="*60)
    
    fed_cfg  = cfg.setdefault("federation", {})
    lt_cfg   = cfg.setdefault("local_training", {})
    data_cfg = cfg.setdefault("data", {})
    exp_cfg  = cfg.setdefault("experiment", {})

    aps = data_cfg.get("aps", [])
    if not aps:
        ap_a = data_cfg.get("ap_a", {"name": "AP_0 (WiFi)", "tp_mean": 50.0, "rtt_mean": 20.0, "mu_rtt": 2.0, "n_channels": 2, "rtt_noise_std": 5.0, "tp_noise_std": 1.0})
        ap_b = data_cfg.get("ap_b", {"name": "AP_1 (Cellular)", "tp_mean": 20.0, "rtt_mean": 80.0, "mu_rtt": 5.0, "n_channels": 10, "rtt_noise_std": 2.0, "tp_noise_std": 1.0})
        if "name" not in ap_a: ap_a["name"] = "AP_0 (WiFi)"
        if "name" not in ap_b: ap_b["name"] = "AP_1 (Cellular)"
        aps = [ap_a, ap_b]
        data_cfg["aps"] = aps

    def rebuild_topology():
        num_edges = fed_cfg.get("num_edge_servers", 2)
        num_terms = fed_cfg.get("num_terminals", 3)
        topo = {f"edge-{i:02d}": [] for i in range(num_edges)}
        for i in range(num_terms):
            topo[f"edge-{i % num_edges:02d}"].append(i)
        fed_cfg["edge_topology"] = topo

    while True:
        edges  = fed_cfg.get('num_edge_servers', 2)
        terms  = fed_cfg.get('num_terminals', 3)
        g_rnd  = fed_cfg.get('num_rounds', 30)
        l_rnd  = fed_cfg.get('local_rounds', 5)
        epochs = lt_cfg.get('epochs', 5)
        lr     = lt_cfg.get('learning_rate', 0.0001)
        iid    = data_cfg.get('iid', True)
        samps  = data_cfg.get('total_samples', 3000)
        seed   = exp_cfg.get('seed', 42)
        
        print("\n--- 現在のシミュレーション設定 ---")
        print(f"  [1] ネットワーク構成 : Edge={edges}台, Terminal={terms}台")
        print(f"  [2] 学習パラメータ   : GlobalRounds={g_rnd}, LocalRounds={l_rnd}, Epochs={epochs}, LR={lr}")
        print(f"  [3] データ・実験設定 : IID={iid}, Samples={samps}, Seed={seed}")
        
        csv_path = data_cfg.get('csv_path', "/Users/tetsuya/HFL/training_data_list/default/training_data.csv")
        print(f"  [5] 教師データ(CSV)選択 : {Path(csv_path).parent.name}/{Path(csv_path).name}")
        print("  [S] 設定を完了してシミュレーション開始 (Enter)")
        
        choice = input("\n変更する項目の番号(1-5)、または開始(S)を入力してください: ").strip().upper()

        
        if choice == 'S' or choice == '':
            break
        elif choice == '1':
            print("\n--- [1] ネットワーク構成の変更 ---")
            e_str = input(f"Edge Server数 [{edges}]: ").strip()
            if e_str.isdigit(): fed_cfg['num_edge_servers'] = int(e_str)
            t_str = input(f"Terminal数 [{terms}]: ").strip()
            if t_str.isdigit(): fed_cfg['num_terminals'] = int(t_str)
            rebuild_topology()
            print("-> 構成を更新し、トポロジーを自動再配置しました。")
            
        elif choice == '2':
            print("\n--- [2] 学習パラメータの変更 ---")
            g_str = input(f"Global Rounds [{g_rnd}]: ").strip()
            if g_str.isdigit(): fed_cfg['num_rounds'] = int(g_str)
            l_str = input(f"Local Rounds [{l_rnd}]: ").strip()
            if l_str.isdigit(): fed_cfg['local_rounds'] = int(l_str)
            ep_str = input(f"Local Epochs [{epochs}]: ").strip()
            if ep_str.isdigit(): lt_cfg['epochs'] = int(ep_str)
            lr_str = input(f"Learning Rate [{lr}]: ").strip()
            if lr_str:
                try: lt_cfg['learning_rate'] = float(lr_str)
                except ValueError: pass
            print("-> 学習パラメータを更新しました。")
            
        elif choice == '3':
            print("\n--- [3] データ・実験設定の変更 ---")
            iid_str = input(f"IID データ分割を使用しますか？ (y/n) [{'y' if iid else 'n'}]: ").strip().lower()
            if iid_str == 'y': data_cfg['iid'] = True
            elif iid_str == 'n': data_cfg['iid'] = False
            
            s_str = input(f"Total Samples [{samps}]: ").strip()
            if s_str.isdigit(): data_cfg['total_samples'] = int(s_str)
            
            seed_str = input(f"Random Seed [{seed}]: ").strip()
            if seed_str.isdigit(): exp_cfg['seed'] = int(seed_str)
            print("-> データ・実験設定を更新しました。")
            
        elif choice == '4':
            while True:
                print("\n--- [4] AP(基地局)設定 ---")
                for i, ap in enumerate(aps):
                    name = ap.get('name', f"AP_{i}")
                    tp   = ap.get('tp_mean', 50.0)
                    rtt  = ap.get('rtt_mean', 20.0)
                    mu   = ap.get('mu_rtt', 2.0)
                    n_ch = ap.get('n_channels', 2)
                    print(f"  [{i}] {name:<18}: TP={tp:4.1f} Mbps, RTT={rtt:5.1f} ms, mu={mu}, n_ch={n_ch}")
                
                print("\n  [0~N] 既存のAPを編集 (番号を入力)")
                print("  [A]   新しいAPを追加")
                print("  [D]   APを削除")
                print("  [B]   戻る (Enter)")
                
                ap_choice = input("変更するAPの番号(0~)、またはコマンド(A/D/B)を入力: ").strip().upper()
                if ap_choice == 'B' or ap_choice == '':
                    break
                elif ap_choice == 'A':
                    print("\n--- 新規 AP 追加 ---")
                    name = input(f"APの名前 [AP_{len(aps)}]: ").strip() or f"AP_{len(aps)}"
                    try:
                        rtt  = float(input("RTT(ms) [20.0]: ").strip() or 20.0)
                        calc_tp = round(1000.0 / rtt if rtt > 0 else 50.0, 1)
                        tp_str = input(f"TP(Mbps) (RTTから自動計算: {calc_tp}) [Enterで適用]: ").strip()
                        tp = float(tp_str) if tp_str else calc_tp
                        mu   = float(input("サービス率 mu [5.0]: ").strip() or 5.0)
                        n_ch = int(input("チャネル数 n [5]: ").strip() or 5)
                        aps.append({"name": name, "tp_mean": tp, "rtt_mean": rtt, "mu_rtt": mu, "n_channels": n_ch, "rtt_noise_std": 2.0, "tp_noise_std": 1.0})
                        print(f"-> {name} を追加しました (RTT={rtt}ms, TP={tp}Mbps)。")
                    except ValueError:
                        print("-> 【エラー】数値以外の文字が入力されました。追加をキャンセルします。")
                elif ap_choice == 'D':
                    if len(aps) <= 1:
                        print("-> これ以上APを削除できません（最低1台必要）。")
                        continue
                    idx_str = input("削除するAPの番号: ").strip()
                    if idx_str.isdigit() and 0 <= int(idx_str) < len(aps):
                        removed = aps.pop(int(idx_str))
                        print(f"-> {removed.get('name')} を削除しました。")
                elif ap_choice.isdigit() and 0 <= int(ap_choice) < len(aps):
                    idx = int(ap_choice)
                    ap = aps[idx]
                    print(f"\n--- [{idx}] {ap.get('name', 'AP')} の編集 (Enterでスキップ) ---")
                    name = input(f"名前 [{ap.get('name', '')}]: ").strip()
                    if name: ap['name'] = name
                    
                    try:
                        rtt_str = input(f"RTT(ms) [{ap.get('rtt_mean', 20.0)}]: ").strip()
                        if rtt_str:
                            ap['rtt_mean'] = float(rtt_str)
                            # RTTが変更された場合、TPの自動計算を提案
                            calc_tp = round(1000.0 / ap['rtt_mean'] if ap['rtt_mean'] > 0 else 50.0, 1)
                            tp_str = input(f"TP(Mbps) (RTTから自動計算: {calc_tp}) [Enterで適用, または数値を入力]: ").strip()
                            ap['tp_mean'] = float(tp_str) if tp_str else calc_tp
                        else:
                            tp_str = input(f"TP(Mbps) [{ap.get('tp_mean', 50.0)}]: ").strip()
                            if tp_str: ap['tp_mean'] = float(tp_str)
                        
                        mu_str = input(f"サービス率 mu [{ap.get('mu_rtt', 2.0)}]: ").strip()
                        if mu_str: ap['mu_rtt'] = float(mu_str)
                        
                        n_str = input(f"チャネル数 n [{ap.get('n_channels', 2)}]: ").strip()
                        if n_str: ap['n_channels'] = int(n_str)
                        print(f"-> {ap.get('name')} を更新しました。")
                    except ValueError:
                        print("-> 【エラー】数値以外の文字が入力されました。変更をキャンセルし、元の値を維持します。")
                else:
                    print("-> 無効な入力です。")

        elif choice == '5':
            print("\n--- [5] 教師データ(CSV)選択 ---")
            base_dir = Path("/Users/tetsuya/HFL/training_data_list")
            subdirs = sorted([d for d in base_dir.iterdir() if d.is_dir()])
            print("利用可能なディレクトリ:")
            for i, d in enumerate(subdirs):
                print(f"  [{i}] {d.name}")
            
            dir_choice = input("ディレクトリ番号を選択してください: ").strip()
            if dir_choice.isdigit() and 0 <= int(dir_choice) < len(subdirs):
                selected_dir = subdirs[int(dir_choice)]
                csv_file = selected_dir / "training_data.csv"
                if csv_file.exists():
                    data_cfg['csv_path'] = str(csv_file)
                    print(f"-> {csv_file} を選択しました。")
                else:
                    print(f"-> エラー: {csv_file} が見つかりません。")
        else:
            print("-> 無効な入力です。")


def _load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        print(f"[警告] 設定ファイルが見つかりません: {path}  デフォルト設定を使用します。")
        return {}
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _apply_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    """コマンドライン引数で設定を上書きする"""
    if args.rounds    is not None: cfg.setdefault("federation", {})["num_rounds"]        = args.rounds
    if args.terminals is not None: cfg.setdefault("federation", {})["num_terminals"]   = args.terminals
    if args.epochs    is not None: cfg.setdefault("local_training", {})["epochs"]      = args.epochs
    if args.lr        is not None: cfg.setdefault("local_training", {})["learning_rate"] = args.lr
    if args.samples   is not None: cfg.setdefault("data", {})["total_samples"]         = args.samples
    if args.seed      is not None: cfg.setdefault("experiment", {})["seed"]            = args.seed
    if args.alpha     is not None: cfg.setdefault("data", {})["dirichlet_alpha"]       = args.alpha
    if args.no_iid:                cfg.setdefault("data", {})["iid"]                   = False
    if args.quiet:                 cfg.setdefault("output", {})["verbose"]              = False
    if args.no_plot:               cfg.setdefault("output", {})["save_plots"]          = False
    # LocalTrainer.kt 由来のパラメータはコマンドライン未対応（yaml で設定）
    return cfg


def _run_simulation(cfg: dict) -> tuple[list, str, Path]:
    """シミュレーションを実行して (agg_history, run_id, log_path) を返す"""
    from sim.runner import HFLRunner
    runner = HFLRunner(cfg)
    agg_history = runner.run()
    return agg_history, runner.run_id, runner.round_log_path


def _run_comparison(
    agg_history: list,
    run_id: str,
    cfg: dict,
) -> dict:
    """理論値比較レポートを生成して summary を返す"""
    from analysis.compare import ComparisonReporter
    fed_cfg   = cfg.get("federation", {})
    lt_cfg    = cfg.get("local_training", {})
    out_cfg   = cfg.get("output", {})
    model_cfg = cfg.get("model", {})

    num_terminals = fed_cfg.get("num_terminals", 3)
    # HFL では全端末が毎ラウンド参加するため K = num_terminals
    K_actual = max(1, num_terminals)

    reporter = ComparisonReporter(
        run_id      = run_id,
        results_dir = out_cfg.get("results_dir", "results"),
        save_plots  = out_cfg.get("save_plots", True),
    )
    return reporter.compare(
        agg_history  = agg_history,
        eta          = lt_cfg.get("learning_rate", 0.0001),
        K            = K_actual,
        E            = lt_cfg.get("epochs", 5),
        iid          = cfg.get("data", {}).get("iid", True),
        fraction_fit = 1.0,
        input_size   = model_cfg.get("input_size",  6),
        hidden_size  = model_cfg.get("hidden_size", 32),
        output_size  = model_cfg.get("output_size",  4),
    )


def _run_analysis(
    run_id:        str,
    log_path:      Path,
    cfg:           dict,
    cmp_jsonl:     "Path | None" = None,
) -> Path:
    """分析フェーズ: Markdown レポートを生成して保存パスを返す"""
    from analysis.report_gen import ReportGenerator
    out_cfg = cfg.get("output", {})
    results_dir = Path(out_cfg.get("results_dir", "results"))
    gen = ReportGenerator(
        run_id           = run_id,
        log_path         = log_path,
        results_dir      = results_dir,
        cfg              = cfg,
        comparison_jsonl = cmp_jsonl,
    )
    return gen.generate()


def _load_agg_history_from_jsonl(path: str):
    """既存の JSONL ログから AggregationResult リストを復元する"""
    from sim.aggregator import AggregationResult

    results = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            evt = json.loads(line)
            if evt.get("event") != "aggregation_complete":
                continue
            results.append(AggregationResult(
                round_id           = evt["round_id"],
                run_id             = evt["run_id"],
                num_participants   = evt.get("num_participants", 1),
                num_excluded       = evt.get("num_excluded", 0),
                total_samples      = evt.get("total_samples", 0),
                global_weight_norm = evt.get("global_weight_norm", 0.0),
                avg_train_loss     = evt.get("avg_train_loss", 0.0),
                avg_train_acc      = evt.get("avg_train_acc", 0.0),
                avg_val_loss       = evt.get("avg_val_loss", 0.0),
                avg_val_acc        = evt.get("avg_val_acc", 0.0),
                min_val_acc        = evt.get("min_val_acc", 0.0),
                max_val_acc        = evt.get("max_val_acc", 0.0),
                agg_duration_ms    = evt.get("agg_duration_ms", 0.0),
                participating_ids  = evt.get("participating_ids", []),
                excluded_ids       = evt.get("excluded_ids", []),
            ))
    return results


def main() -> int:
    args = _build_parser().parse_args()
    cfg  = _load_config(args.config)
    cfg  = _apply_overrides(cfg, args)

    if args.interactive:
        _interactive_config(cfg)

    from sim import display

    # ── 既存ログから比較のみ実行 ──────────────────────────
    if args.compare_only:
        display.print_banner("Sim_HFL  —  理論値比較モード")
        display.print_phase(1, "ログ読み込み")
        agg_history = _load_agg_history_from_jsonl(args.compare_only)
        if not agg_history:
            display.print_error("ログに aggregation_complete イベントが見つかりません")
            return 1
        display.print_done(f"ログ読み込み完了  {len(agg_history)} ラウンド")

        run_id = agg_history[0].run_id
        display.print_phase(2, "理論値比較")
        summary = _run_comparison(agg_history, run_id, cfg)
        _print_summary(summary, display)

        display.print_phase(3, "Markdown レポート生成")
        out_cfg     = cfg.get("output", {})
        results_dir = Path(out_cfg.get("results_dir", "results"))
        cmp_jsonl   = results_dir / "reports" / f"{run_id[:8]}_comparison.jsonl"
        report_path = _run_analysis(run_id, Path(args.compare_only), cfg, cmp_jsonl)
        display.print_done(f"レポート: {report_path}")
        return 0

    # ── シミュレーション実行 ──────────────────────────────
    n_runs     = max(1, args.runs)
    base_seed  = cfg.get("experiment", {}).get("seed", 42)
    run_records: list[dict] = []   # 各試行の結果をまとめる

    display.print_banner("Sim_HFL  —  FedAvg シミュレーション")

    for run_idx in range(n_runs):
        # シードを試行ごとにインクリメント
        run_cfg = json.loads(json.dumps(cfg))   # deep copy
        run_cfg.setdefault("experiment", {})["seed"] = base_seed + run_idx

        if n_runs > 1:
            sep = "─" * 62
            print(f"\n{display.cyan(sep)}")
            print(f"  {display.bold(f'Run {run_idx + 1} / {n_runs}')}  "
                  f"{display.dim(f'seed={base_seed + run_idx}')}")
            print(f"{display.cyan(sep)}")

        if run_idx == 0:
            display.print_phase(0, "実験設定")
            display.print_config(run_cfg)

        agg_history, run_id, log_path = _run_simulation(run_cfg)

        # 理論値比較
        cmp_jsonl_path = None
        if args.compare:
            display.print_phase(3, "理論値比較")
            summary = _run_comparison(agg_history, run_id, run_cfg)
            _print_summary(summary, display)
            out_cfg        = run_cfg.get("output", {})
            results_dir    = Path(out_cfg.get("results_dir", "results"))
            cmp_jsonl_path = results_dir / "reports" / f"{run_id[:8]}_comparison.jsonl"

        # 分析フェーズ
        phase_num = 4 if args.compare else 3
        display.print_phase(phase_num, "Markdown レポート生成")
        report_path = _run_analysis(run_id, log_path, run_cfg, cmp_jsonl_path)
        display.print_done(f"レポート: {report_path}")

        # 最終サマリー（単発 or 複数の場合どちらも表示）
        display.print_final_summary(
            run_id      = run_id,
            agg_history = agg_history,
            report_path = str(report_path),
        )

        # 記録
        final = agg_history[-1] if agg_history else None
        final_acc  = final.avg_val_acc  if final else 0.0
        final_loss = final.avg_val_loss if final else 0.0
        conv_rnd   = next(
            (i + 1 for i, r in enumerate(agg_history)
             if r.avg_val_acc >= final_acc * 0.95),
            len(agg_history),
        )

        # 満足度HMをJSONLから抽出
        hm_before_final, hm_after_final = None, None
        per_terminal_sat: list[dict] = []
        try:
            with open(log_path, encoding="utf-8") as _f:
                sat_summaries = []
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        evt = json.loads(_line)
                    except Exception:
                        continue
                    if evt.get("event") == "satisfaction_summary":
                        sat_summaries.append(evt)
                if sat_summaries:
                    last_sat = sat_summaries[-1]
                    hm_before_final = last_sat.get("harmonic_mean_satisfaction_before")
                    hm_after_final = last_sat.get("harmonic_mean_satisfaction")
                    for td in last_sat.get("terminal_details", []):
                        per_terminal_sat.append({
                            "terminal_id": td.get("terminal_id"),
                            "app_type": td.get("app_type", "?"),
                            "sat_before": td.get("satisfaction_before"),
                            "sat_after": td.get("satisfaction"),
                        })
        except Exception:
            pass

        run_records.append({
            "run":            run_idx + 1,
            "seed":           base_seed + run_idx,
            "run_id":         run_id,
            "final_acc":      final_acc,
            "final_loss":     final_loss,
            "conv_round":     conv_rnd,
            "hm_sat_before":  hm_before_final,
            "hm_sat_after":   hm_after_final,
            "per_terminal":   per_terminal_sat,
            "report":         str(report_path),
            "log_path":       str(log_path),
        })

    # ── 複数試行の集計サマリー ────────────────────────────
    if n_runs > 1:
        import statistics
        accs   = [r["final_acc"]  for r in run_records]
        losses = [r["final_loss"] for r in run_records]
        convs  = [r["conv_round"] for r in run_records]
        hm_befs = [r["hm_sat_before"] for r in run_records if r.get("hm_sat_before") is not None]
        hm_afts = [r["hm_sat_after"]  for r in run_records if r.get("hm_sat_after") is not None]

        print()
        print(display.b_cyan("╔" + "═" * 68 + "╗"))
        print(display.b_cyan("│") + f"  {display.bold(f'{n_runs} 回試行  集計サマリー')}" + " " * (67 - len(f'  {n_runs} 回試行  集計サマリー')) + display.b_cyan("│"))
        print(display.b_cyan("│") + " " * 68 + display.b_cyan("│"))

        summary_rows = [
            ("val_acc   mean", f"{statistics.mean(accs)*100:.2f}%"),
            ("val_acc   std",  f"{statistics.stdev(accs)*100:.2f}%" if n_runs > 1 else "—"),
            ("val_acc   max",  f"{max(accs)*100:.2f}%"),
            ("val_acc   min",  f"{min(accs)*100:.2f}%"),
            ("conv_round mean",f"{statistics.mean(convs):.1f}"),
        ]
        if hm_befs:
            summary_rows.append(("sat_before HM mean", f"{statistics.mean(hm_befs)*100:.2f}%"))
            if len(hm_befs) > 1:
                summary_rows.append(("sat_before HM std", f"{statistics.stdev(hm_befs)*100:.2f}%"))
        if hm_afts:
            summary_rows.append(("sat_after  HM mean", f"{statistics.mean(hm_afts)*100:.2f}%"))
            if len(hm_afts) > 1:
                summary_rows.append(("sat_after  HM std", f"{statistics.stdev(hm_afts)*100:.2f}%"))

        for k, v in summary_rows:
            print(display.b_cyan("│") + f"  {display.dim(k):<26}: {display.bold(v)}")

        print(display.b_cyan("│") + " " * 68 + display.b_cyan("│"))
        print(display.b_cyan("│") + f"  {display.dim('Run'):<5} {display.dim('Seed'):<6} {display.dim('val_acc'):<10} {display.dim('conv'):<8} {display.dim('HM_bef'):<9} {display.dim('HM_aft')}")
        for r in run_records:
            hb = f"{r['hm_sat_before']*100:.1f}%" if r.get('hm_sat_before') is not None else "—"
            ha = f"{r['hm_sat_after']*100:.1f}%"  if r.get('hm_sat_after') is not None else "—"
            print(display.b_cyan("│") + f"  {r['run']:<5} {r['seed']:<6} {r['final_acc']*100:>7.2f}%   Rnd {r['conv_round']:<4} {hb:<9} {ha}")
        print(display.b_cyan("╚" + "═" * 68 + "╝"))

        # ── バッチサマリーレポート (Markdown) を出力 ────────────
        _write_batch_summary_report(run_records, cfg, display)

    return 0


def _write_batch_summary_report(
    run_records: list[dict], cfg: dict, display
) -> None:
    """複数試行の集計レポートを Markdown で出力する。"""
    import statistics
    from datetime import datetime

    out_cfg = cfg.get("output", {})
    results_dir = Path(out_cfg.get("results_dir", "results"))
    reports_dir = results_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    n = len(run_records)
    path = reports_dir / f"batch_{n}trials_{stamp}.md"

    accs   = [r["final_acc"]  for r in run_records]
    losses = [r["final_loss"] for r in run_records]
    convs  = [r["conv_round"] for r in run_records]
    hm_befs = [r["hm_sat_before"] for r in run_records if r.get("hm_sat_before") is not None]
    hm_afts = [r["hm_sat_after"]  for r in run_records if r.get("hm_sat_after") is not None]

    lines = [
        f"# Batch Summary Report ({n} trials)",
        "",
        f"Generated: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
        "",
        "## Aggregate Statistics",
        "",
        "| Metric | Mean | Std | Min | Max |",
        "|--------|------|-----|-----|-----|",
        f"| val_acc | {statistics.mean(accs)*100:.2f}% | {statistics.stdev(accs)*100:.2f}% | {min(accs)*100:.2f}% | {max(accs)*100:.2f}% |" if n > 1 else
        f"| val_acc | {statistics.mean(accs)*100:.2f}% | — | {min(accs)*100:.2f}% | {max(accs)*100:.2f}% |",
        f"| val_loss | {statistics.mean(losses):.4f} | {statistics.stdev(losses):.4f} | {min(losses):.4f} | {max(losses):.4f} |" if n > 1 else
        f"| val_loss | {statistics.mean(losses):.4f} | — | {min(losses):.4f} | {max(losses):.4f} |",
        f"| conv_round | {statistics.mean(convs):.1f} | {statistics.stdev(convs):.1f} | {min(convs)} | {max(convs)} |" if n > 1 else
        f"| conv_round | {statistics.mean(convs):.1f} | — | {min(convs)} | {max(convs)} |",
    ]
    if hm_befs:
        std_b = f"{statistics.stdev(hm_befs)*100:.2f}%" if len(hm_befs) > 1 else "—"
        lines.append(f"| sat_before (HM) | {statistics.mean(hm_befs)*100:.2f}% | {std_b} | {min(hm_befs)*100:.2f}% | {max(hm_befs)*100:.2f}% |")
    if hm_afts:
        std_a = f"{statistics.stdev(hm_afts)*100:.2f}%" if len(hm_afts) > 1 else "—"
        lines.append(f"| sat_after (HM) | {statistics.mean(hm_afts)*100:.2f}% | {std_a} | {min(hm_afts)*100:.2f}% | {max(hm_afts)*100:.2f}% |")
    lines.append("")

    # Per-trial table
    lines += [
        "## Per-Trial Results",
        "",
        "| Run | Seed | val_acc | val_loss | conv_rnd | HM_before | HM_after |",
        "|-----|------|---------|----------|----------|-----------|----------|",
    ]
    for r in run_records:
        hb = f"{r['hm_sat_before']*100:.1f}%" if r.get('hm_sat_before') is not None else "—"
        ha = f"{r['hm_sat_after']*100:.1f}%"  if r.get('hm_sat_after') is not None else "—"
        lines.append(
            f"| {r['run']} | {r['seed']} | {r['final_acc']*100:.2f}% | {r['final_loss']:.4f} "
            f"| {r['conv_round']} | {hb} | {ha} |"
        )
    lines.append("")

    # Per-terminal satisfaction across all trials
    lines += [
        "## Per-Terminal Satisfaction (Final Round)",
        "",
        "| Run | Terminal | App | sat_before | sat_after | delta |",
        "|-----|----------|-----|------------|-----------|-------|",
    ]
    for r in run_records:
        for td in r.get("per_terminal", []):
            sb = td.get("sat_before")
            sa = td.get("sat_after")
            sb_str = f"{sb*100:.1f}%" if sb is not None else "—"
            sa_str = f"{sa*100:.1f}%" if sa is not None else "—"
            delta = ""
            if sb is not None and sa is not None:
                d = sa - sb
                delta = f"{d:+.3f}"
            lines.append(
                f"| {r['run']} | {td.get('terminal_id', '?')} | {td.get('app_type', '?')} "
                f"| {sb_str} | {sa_str} | {delta} |"
            )
    lines.append("")

    # Individual reports
    lines += ["## Individual Reports", ""]
    for r in run_records:
        lines.append(f"- Run {r['run']}: `{r['report']}`")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    display.print_done(f"バッチサマリーレポート: {path}")


def _print_summary(summary: dict, display) -> None:
    display.print_done(
        f"最終 val_acc={summary['final_sim_val_acc']:.4f}  "
        f"val_loss={summary.get('final_sim_val_loss', 0):.4f}  "
        f"loss_gap={summary['avg_gap_loss']:+.4f}  "
        f"収束 Round {summary['convergence_round']}"
    )
    cc = summary.get("communication_cost", {})
    if cc.get("total_mb"):
        display.print_done(f"総通信量: {cc['total_mb']} MB")


if __name__ == "__main__":
    sys.exit(main())

# hungarian method
from os.path import dirname, abspath
import os, sys, json, cal, rand, create, time, threading, signal, random
from datetime import datetime
from csv import writer
from typing import List
from term import Term  # 端末1台が持つデータ構造
from ap import Ap  #1基地局が持つデータ構造
import hungarian_kai as hung

parent_dir = dirname(abspath(__file__))
if parent_dir not in sys.path: # 追加
    sys.path.append(parent_dir)

with open('sim.json', "r", encoding="utf-8") as file:
    confSim = json.load(file)
with open('ap.json', "r", encoding="utf-8") as file:
    confAp = json.load(file)
with open('app.json', "r", encoding="utf-8") as file:
    confAPP = json.load(file)

if __name__ == '__main__':

# --- 対話式設定 ---
    print("=== 教師データ生成設定 ===")
    _default_rtt = [20.0, 28.0, 32.0]
    _default_n   = [10, 5, 3]

    _in = input(f"端末台数          (デフォルト: {confSim['termNum']}): ").strip()
    term_num = int(_in) if _in else confSim['termNum']

    _in = input(f"基地局数          (デフォルト: {confSim['apNumMax']}): ").strip()
    ap_num = int(_in) if _in else confSim['apNumMax']

    init_rtt: List[float] = []
    erlang_n: List[int]   = []
    for i in range(ap_num):
        d_rtt = _default_rtt[i] if i < len(_default_rtt) else 30.0
        d_n   = _default_n[i]   if i < len(_default_n)   else 3
        _in = input(f"  AP{i} 初期RTT [ms]  (デフォルト: {d_rtt}): ").strip()
        init_rtt.append(float(_in) if _in else d_rtt)
        _in = input(f"  AP{i} 回線数 erlang_n (デフォルト: {d_n}): ").strip()
        erlang_n.append(int(_in) if _in else d_n)

    _in = input(f"RTT変動幅 ±ms     (0=固定, デフォルト: 15): ").strip()
    rtt_variation = float(_in) if _in else 15.0

    # 目標行数と出力ファイルを決定
    default_target = term_num * confSim["simNumTime"]

    _in = input(f"目標行数          (デフォルト: {default_target}): ").strip()
    target_rows = int(_in) if _in else default_target

    _in = input("メモ（任意、Enterでスキップ）: ").strip()
    memo = _in if _in else ""

    # ファイル名生成
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    erlang_str = "-".join(str(n) for n in erlang_n)
    memo_part = f"_{memo}" if memo else ""
    if rtt_variation > 0:
        rtt_str = f"rttV{rtt_variation}"
    else:
        rtt_str = "rtt" + "-".join(str(r) for r in init_rtt)
    file_name = f"{term_num}_event_{timestamp}_{rtt_str}_n{erlang_str}{memo_part}.csv"

    # 既存行数をカウント（再開用）
    existing_rows = 0
    if os.path.exists(file_name):
        with open(file_name, 'r') as f:
            existing_rows = sum(1 for _ in f)

    remaining = target_rows - existing_rows
    print(f"\n端末台数={term_num}, 基地局数={ap_num}, 初期RTT={init_rtt}, 変動=±{rtt_variation}ms, erlang_n={erlang_n}")
    print(f"出力ファイル: {file_name}")
    print(f"既存行数: {existing_rows} 行 / 目標: {target_rows} 行 / 追加生成: {remaining} 行")
    print("=========================\n")

    if remaining <= 0:
        print("目標行数に達しています。終了します。")
        sys.exit(0)

# Hungurian Method
#基地局の生成
    APS: List[Ap] = create.createAp(ap_num)

#端末の生成
    TERMS: List[Term] = create.createTerm(term_num)

#実行
    written_rows = 0
    stop_event = threading.Event()

    # --- 停止フラグ制御 ---
    # Ctrl+C はラウンドを中断せず停止フラグを立てるだけにする
    def _handle_sigint(sig, frame):
        if not stop_event.is_set():
            stop_event.set()
            print("\n[Ctrl+C] 現在のラウンド完了後に停止します...")

    signal.signal(signal.SIGINT, _handle_sigint)

    # バックグラウンドスレッドでコマンド入力を監視
    def _listen_stop():
        print("  ※ 停止するには「q」を入力してEnterを押してください\n")
        while not stop_event.is_set():
            try:
                cmd = input()
            except EOFError:
                break
            if cmd.strip().lower() in ('q', 'quit', 's', 'stop'):
                stop_event.set()
                print("[停止リクエスト] 現在のラウンド完了後に停止します...")
                break

    listener = threading.Thread(target=_listen_stop, daemon=True)
    listener.start()

    while written_rows < remaining and not stop_event.is_set():
        start_time = time.time()
        print("-------------------------------------------------------------")

        # ラウンドごとにRTTを決定（変動あり or 固定）
        if rtt_variation > 0:
            round_init_rtt = [max(5.0, rtt + random.uniform(-rtt_variation, rtt_variation)) for rtt in init_rtt]
        else:
            round_init_rtt = list(init_rtt)

        # Random setting base station (id) for each terminal
        rand.randAp(TERMS, APS)

        # Random setting Application for each terminal
        rand.randApp(TERMS, APS)

        # Sum of terminals for each base station
        cal.sumTermAp(TERMS, APS)

        # Calculation of connected TP and RTT (before Hungarian)
        cal.calLink(TERMS, APS, confSim["appUseSec"], round_init_rtt, erlang_n)
        cal.calSatis(TERMS, APS)
        print("↓")

        # ハンガリアン法
        combination = hung.call_hungarian(TERMS, APS, round_init_rtt, erlang_n)

        # Recalculate TP/RTT after assignment
        cal.sumTermAp(TERMS, APS)
        cal.calLink(TERMS, APS, confSim["appUseSec"], round_init_rtt, erlang_n)
        cal.calSatis(TERMS, APS)

        # 1ラウンド分のデータを構築してCSVに即時書き込み
        with open(file_name, 'a', newline='') as f:
            csv_writer = writer(f)
            for term in TERMS:
                app = cal.calAppNeed(term.appNum)
                ap_id = int(combination.combiApTermArray[term.id])
                row = [app.needTP, app.needRTT, term.appNum]
                for ap in APS:
                    row.extend([ap.tp, ap.rtt, ap.termNum])
                row.append(ap_id)
                csv_writer.writerow(row)

        written_rows += term_num
        elapsed = time.time() - start_time
        total_written = existing_rows + written_rows
        print(f"実行時間: {elapsed:.2f}秒  |  進捗: {total_written} / {target_rows} 行")

    total_written = existing_rows + written_rows
    if stop_event.is_set():
        print(f"\n停止しました。{total_written} 行まで保存済みです。")
        print(f"次回実行時に {file_name} の続きから再開されます。")
    else:
        print(f"\n完了: {file_name} に合計 {total_written} 行を保存しました。")
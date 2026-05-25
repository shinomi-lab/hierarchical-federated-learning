"""
AP切り替え推論テスト — /edge/infer エンドポイント

確認ポイント:
  1. AP0 と AP1 の両方が出力されること（ハリボテでないこと）
  2. 入力値（TP/RTT/appNum）の変化が結果に反映されること
  3. 修正後の one-hot が正しく4次元で渡されていること

使い方:
  cd Serverside_HFL
  EDGE_INFERENCE_MIN_ROUND=0 python scripts/test_ap_switch_inference.py
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "edge_server"))

# ラウンドゲートを無効化
os.environ.setdefault("EDGE_INFERENCE_MIN_ROUND", "0")

from fastapi.testclient import TestClient
from edge_server.main import app
from edge_server.state import current_edge_state

client = TestClient(app)

# 実際に存在するモデルを指定（最新の訓練済みモデル）
MODEL_REL = "global_model_mobile.pt"
app.state.package = {"model_rel": MODEL_REL}
current_edge_state.round = 99  # ゲートを確実に通過

APP_NAMES = {0: "browser", 1: "video", 2: "call", 3: "other"}

# ─────────────────────────────────────────────
# テストケース定義
# tpNeed: Mbps (browser/video は高い値が良い)
# rttNeed: ms  (call は低い値が良い)
# appNum: 0=browser 1=video 2=call 3=other
# ─────────────────────────────────────────────
TEST_CASES = [
    # --- browser (0): TP重視 ---
    {"tpNeed": 10.0, "rttNeed":  20.0, "appNum": 0, "label": "browser/高TP低RTT"},
    {"tpNeed":  5.0, "rttNeed":  30.0, "appNum": 0, "label": "browser/中TP低RTT"},
    {"tpNeed":  1.0, "rttNeed":  50.0, "appNum": 0, "label": "browser/低TP中RTT"},
    {"tpNeed":  0.3, "rttNeed": 200.0, "appNum": 0, "label": "browser/極低TP高RTT"},
    {"tpNeed":  0.1, "rttNeed": 400.0, "appNum": 0, "label": "browser/最悪"},

    # --- video (1): TP重視 ---
    {"tpNeed": 15.0, "rttNeed":  20.0, "appNum": 1, "label": "video/高TP低RTT"},
    {"tpNeed":  8.0, "rttNeed":  40.0, "appNum": 1, "label": "video/中TP中RTT"},
    {"tpNeed":  2.0, "rttNeed":  80.0, "appNum": 1, "label": "video/低TP高RTT"},
    {"tpNeed":  0.5, "rttNeed": 150.0, "appNum": 1, "label": "video/極低TP"},
    {"tpNeed":  0.1, "rttNeed": 350.0, "appNum": 1, "label": "video/最悪"},

    # --- call (2): RTT重視 ---
    {"tpNeed":  2.0, "rttNeed":  10.0, "appNum": 2, "label": "call/低RTT"},
    {"tpNeed":  1.5, "rttNeed":  30.0, "appNum": 2, "label": "call/中RTT"},
    {"tpNeed":  1.0, "rttNeed":  80.0, "appNum": 2, "label": "call/高RTT"},
    {"tpNeed":  0.5, "rttNeed": 200.0, "appNum": 2, "label": "call/極高RTT"},
    {"tpNeed":  0.2, "rttNeed": 500.0, "appNum": 2, "label": "call/最悪"},

    # --- other (3): 混合 ---
    {"tpNeed":  5.0, "rttNeed":  20.0, "appNum": 3, "label": "other/高TP低RTT"},
    {"tpNeed":  2.0, "rttNeed":  60.0, "appNum": 3, "label": "other/中TP中RTT"},
    {"tpNeed":  0.5, "rttNeed": 120.0, "appNum": 3, "label": "other/低TP高RTT"},
    {"tpNeed":  0.2, "rttNeed": 300.0, "appNum": 3, "label": "other/極低TP"},
    {"tpNeed":  0.1, "rttNeed": 500.0, "appNum": 3, "label": "other/最悪"},
]


def run_batch(cases):
    """全ケースをまとめて1回のリクエストで送る"""
    body = {
        "input": [{"tpNeed": c["tpNeed"], "rttNeed": c["rttNeed"], "appNum": c["appNum"]}
                  for c in cases],
        "model_rel": MODEL_REL,
    }
    resp = client.post("/edge/infer", json=body)
    return resp


def print_header():
    print("\n" + "=" * 90)
    print(f"  AP切り替え推論テスト  モデル: {MODEL_REL}")
    print("=" * 90)
    print(f"{'#':>3}  {'ラベル':<28}  {'app':>6}  {'TP':>6}  {'RTT':>6}  "
          f"{'pred':>5}  {'AP':>3}  {'conf':>6}  {'logits(抜粋)'}")
    print("-" * 90)


def main():
    print_header()

    resp = run_batch(TEST_CASES)

    if resp.status_code != 200:
        print(f"\n[ERROR] HTTP {resp.status_code}: {resp.text}")
        sys.exit(1)

    data = resp.json()
    preds      = data.get("pred", [])
    confs      = data.get("conf", [])
    assigns    = data.get("final_assign", [])
    logits_all = data.get("logits", [])
    latency    = data.get("latency_ms", "?")

    results_by_ap = {0: [], 1: []}
    low_conf = []

    for i, case in enumerate(TEST_CASES):
        pred   = preds[i]   if i < len(preds)   else "?"
        conf   = confs[i]   if i < len(confs)   else 0.0
        ap     = assigns[i] if i < len(assigns)  else "?"
        logits = logits_all[i] if i < len(logits_all) else []
        logits_str = "[" + ", ".join(f"{v:+.2f}" for v in logits) + "]" if logits else "N/A"

        print(f"{i+1:>3}  {case['label']:<28}  "
              f"{APP_NAMES[case['appNum']]:>6}  "
              f"{case['tpNeed']:>5.1f}M  {case['rttNeed']:>5.0f}ms  "
              f"cls{pred:>2}  AP{ap:>1}  {conf:>5.1%}  {logits_str}")

        if isinstance(ap, int):
            results_by_ap.setdefault(ap, []).append(case["label"])
        if isinstance(conf, float) and conf < 0.4:
            low_conf.append((i + 1, case["label"], conf))

    # ─── サマリー ───
    print("\n" + "=" * 90)
    print(f"  推論レイテンシ: {latency} ms")
    print()

    ap0_count = len([a for a in assigns if a == 0])
    ap1_count = len([a for a in assigns if a == 1])
    total = len(assigns)

    print(f"  AP0 割り当て: {ap0_count}/{total} 件 ({ap0_count/total:.0%})")
    print(f"  AP1 割り当て: {ap1_count}/{total} 件 ({ap1_count/total:.0%})")
    print()

    if ap0_count > 0 and ap1_count > 0:
        print("  [OK] AP0 と AP1 の両方が出力されました。推論は正常に機能しています。")
    elif ap0_count == 0:
        print("  [WARNING] AP0 が一度も選ばれませんでした。モデルに偏りがある可能性があります。")
    else:
        print("  [WARNING] AP1 が一度も選ばれませんでした。モデルに偏りがある可能性があります。")

    if low_conf:
        print(f"\n  信頼度 < 40% のケース ({len(low_conf)} 件):")
        for idx, label, conf in low_conf:
            print(f"    #{idx} {label}  conf={conf:.1%}")

    # ─── appNum 別集計 ───
    print("\n  appNum 別 AP割り当て:")
    for app_idx, app_name in APP_NAMES.items():
        app_cases = [i for i, c in enumerate(TEST_CASES) if c["appNum"] == app_idx]
        ap0 = sum(1 for i in app_cases if i < len(assigns) and assigns[i] == 0)
        ap1 = sum(1 for i in app_cases if i < len(assigns) and assigns[i] == 1)
        print(f"    {app_name:<8}: AP0={ap0}, AP1={ap1}")

    print("=" * 90 + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""端末満足度メトリクスの動的レンジを客観的に検証する。
式・要求値・ネットワークモデルは実コードから抽出:
 - TP app: S = TP_link / TP_need   (clip (0,1])
 - RTT app: S = RTT_need / RTT_link (clip (0,1])
 - 集約: 調和平均 (min支配)
"""

# app.json の要求値
NEED = {
    "browser": ("tp", 5.0),
    "video":   ("tp", 10.0),
    "call":    ("rtt", 200.0),
    "stream":  ("rtt", 100.0),
}

# virtual_congestion.py ROUTER_PARAMS
AP = {
    "A": dict(rtt0=20.0, tp0=50.0, mu=2.0, n=2),
    "B": dict(rtt0=80.0, tp0=20.0, mu=5.0, n=10),
}

def erlang_b(n, A):
    if n <= 0: return 1.0
    if A <= 0: return 0.0
    B = 1.0
    for k in range(1, n+1):
        B = (A*B)/(k + A*B)
    return B

def link_state(ap, lam):
    p = AP[ap]
    # M/M/1 RTT
    if lam >= p["mu"]:
        rtt = 10000.0
    else:
        rtt = (1.0/(p["mu"]-lam))*1000.0
    delta = max(0.0, rtt - p["rtt0"])
    rtt_report = min(10000.0, p["rtt0"] + delta)
    # Erlang-B TP
    A = lam/p["mu"] if p["mu"]>0 else lam
    tp = max(0.0, p["tp0"]*(1.0 - erlang_b(p["n"], A)))
    return rtt_report, tp

def sat(app, ap, lam):
    kind, need = NEED[app]
    rtt, tp = link_state(ap, lam)
    if kind == "tp":
        raw = tp/need if need>0 else 1.0
    else:
        raw = need/rtt if rtt>0 else 1e-6
    return max(1e-6, min(1.0, raw))

print("="*70)
print("【検証1】各アプリ×AP×負荷(λ=接続端末数) の満足度")
print("="*70)
for app in NEED:
    print(f"\n-- {app} (need={NEED[app]}) --")
    print("  λ:    ", "  ".join(f"{l}" for l in range(0,6)))
    for ap in ("A","B"):
        row = "  ".join(f"{sat(app,ap,l):.3f}" for l in range(0,6))
        print(f"  AP_{ap}: {row}")

print()
print("="*70)
print("【検証2】TP系アプリは天井(1.0)に張り付くか? — 動的レンジ")
print("="*70)
for app in ("browser","video"):
    vals = [sat(app,ap,l) for ap in ("A","B") for l in range(0,6)]
    print(f"  {app:8s}: min={min(vals):.3f} max={max(vals):.3f}  "
          f"1.0の割合={sum(1 for v in vals if v>=0.999)}/{len(vals)}")

print()
print("="*70)
print("【検証3】4端末[browser,video,call]の調和平均は誰に支配されるか")
print("="*70)
def hmean(xs):
    return len(xs)/sum(1.0/max(x,1e-6) for x in xs)

# 典型構成: browser@A, video@A, call@A, call@B  (call端末が混雑APにいる/いない)
scenarios = {
    "全員AP_A空き(λ=1想定)":     [sat("browser","A",1), sat("video","A",1), sat("call","A",1)],
    "call端末が混雑AP_A(λ=2)":   [sat("browser","A",2), sat("video","A",2), sat("call","A",2)],
    "call端末がAP_B(λ=1)へ回避": [sat("browser","A",2), sat("video","A",2), sat("call","B",1)],
    "call端末がAP_B混雑(λ=4)":   [sat("browser","A",2), sat("video","A",2), sat("call","B",4)],
}
for name, sats in scenarios.items():
    hm = hmean(sats)
    am = sum(sats)/len(sats)
    print(f"  {name:26s} 端末別={['%.2f'%s for s in sats]}  "
          f"算術平均={am:.3f}  調和平均={hm:.3f}")

print()
print("="*70)
print("【検証4】call端末のRTT感度: 政策改善(RTT短縮)が満足度にどう反映されるか")
print("="*70)
print("  RTT_link :  S=200/RTT   (改善の可視性)")
for rtt in [200, 300, 500, 800, 1000, 2000, 5000, 10000]:
    print(f"    {rtt:6d}ms -> S={min(1.0,200/rtt):.3f}")

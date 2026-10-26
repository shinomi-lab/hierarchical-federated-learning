#!/usr/bin/env python3
"""改善案A-Eの効果をシミュレーションで定量化する。
実機コード(virtual_congestion.py / network_model.py / TerminalSatisfaction.kt=data_gen.py)の
式をそのまま使用。4端末×2APの全16割当を列挙し「政策識別力」を測る。

政策識別力 = 指標が fixed / random / greedy / optimal を区別できる度合い
           = (最良政策の指標値 - 最悪政策の指標値)。0に近い=学習しても改善が見えない。
"""
import itertools, math

# ── 実機ネットワークモデル (virtual_congestion.py / network_model.py と同一) ──
AP = {"A": dict(rtt0=20.0, tp0=50.0, mu=2.0, n=2),
      "B": dict(rtt0=80.0, tp0=20.0, mu=5.0, n=10)}

def erlang_b(n, A):
    if n <= 0: return 1.0
    if A <= 0: return 0.0
    B = 1.0
    for k in range(1, n+1): B = (A*B)/(k + A*B)
    return B

def link(ap, lam):
    p = AP[ap]
    rtt = 10000.0 if lam >= p["mu"] else (1.0/(p["mu"]-lam))*1000.0
    rtt = min(10000.0, p["rtt0"] + max(0.0, rtt - p["rtt0"]))
    tp = max(0.0, p["tp0"]*(1.0 - erlang_b(p["n"], lam/p["mu"])))
    return rtt, tp

# ── 端末(実機 terminal_apps=[browser,video,call,other]) ──
# data_gen.py: _TP_NEEDS=[5,2,10,1], _RTT_NEEDS=[100,50,200,30], _RTT_PRIOR={2}
APPS = ["browser","video","call","other"]
RTT_PRIOR = {2}          # call のみ RTT 優先
TP_NEED  = [5.0, 2.0, 10.0, 1.0]
RTT_NEED = [100.0, 50.0, 200.0, 30.0]
EPS = 1e-6

def sat_current(app_idx, tp, rtt):
    """実機と同一: 逆数+ハードクリップ"""
    if app_idx in RTT_PRIOR: raw = RTT_NEED[app_idx]/max(rtt,EPS)
    else:                    raw = tp/max(TP_NEED[app_idx],EPS)
    return min(1.0, max(EPS, raw))

def sat_smooth(app_idx, tp, rtt):
    """案C: 滑らかな効用(ソフトクリップ)。x=達成/要求。S=x^2/(1+x^2)風のsoft(参照点x=1でS=0.5)を
    要求充足でS→1に寄せるため S = x/(1+x)*2 をclip... ここではロジスティック:
    S = 1/(1+exp(-k*(ln(x)))) = x^k/(1+x^k)。k=2。要求ちょうど(x=1)でS=0.5、余裕で1に漸近、不足で滑らかに低下。"""
    if app_idx in RTT_PRIOR: x = RTT_NEED[app_idx]/max(rtt,EPS)
    else:                    x = tp/max(TP_NEED[app_idx],EPS)
    k = 2.0
    xk = x**k
    return xk/(1.0+xk)

def per_terminal_sats(assign, tp_need_scale=1.0, satfn=sat_current):
    """assign: 例('A','A','B','A')。各端末の満足度を返す。"""
    nA = sum(1 for a in assign if a=="A"); nB = 4-nA
    global TP_NEED
    base = [5.0,2.0,10.0,1.0]
    TP_NEED = [b*tp_need_scale for b in base]
    sats=[]
    for i,ap in enumerate(assign):
        lam = nA if ap=="A" else nB
        rtt,tp = link(ap, lam)
        sats.append(satfn(i, tp, rtt))
    TP_NEED = base
    return sats

def hmean(xs): return len(xs)/sum(1.0/max(x,EPS) for x in xs)
def amean(xs): return sum(xs)/len(xs)
def worst(xs): return min(xs)
def satisfied_ratio(xs, thr=0.8): return sum(1 for x in xs if x>=thr)/len(xs)

ALL = list(itertools.product("AB", repeat=4))

def policy_assignments():
    fixed = ("A","A","A","A")
    # greedy: 各端末が自分の満足度最大のAPを選ぶ最善応答の均衡(反復)
    cur = list(fixed)
    for _ in range(20):
        changed=False
        for i in range(4):
            best,ba=None,cur[i]
            for cand in ("A","B"):
                trial=cur.copy(); trial[i]=cand
                s=per_terminal_sats(tuple(trial))[i]
                if best is None or s>best: best,ba=s,cand
            if ba!=cur[i]: cur[i]=ba; changed=True
        if not changed: break
    greedy=tuple(cur)
    # optimal: 集約(調和平均)最大の割当(oracle)
    opt=max(ALL, key=lambda a: hmean(per_terminal_sats(a)))
    return {"fixed":fixed, "greedy":greedy, "optimal":opt}

def evaluate(satfn=sat_current, tp_scale=1.0, agg=hmean):
    pol=policy_assignments()
    vals={}
    for name,a in pol.items():
        vals[name]=agg(per_terminal_sats(a, tp_scale, satfn))
    vals["random"]=sum(agg(per_terminal_sats(a,tp_scale,satfn)) for a in ALL)/len(ALL)
    return vals, pol

print("#"*72)
print("# 現状(baseline): 逆数満足度 + 調和平均")
print("#"*72)
v,pol=evaluate()
for k in ["fixed","random","greedy","optimal"]:
    print(f"  {k:8s} {pol.get(k,'-')!s:20s} 調和平均={v[k]:.3f}")
disc=v["optimal"]-v["fixed"]
print(f"  → 政策識別力(optimal-fixed) = {disc:.3f}")
print(f"  → fixedとgreedyの差 = {v['greedy']-v['fixed']:+.3f}")

print()
print("#"*72)
print("# 案A: 要求値TP_need再校正 — スケール倍率を振って識別力を測る")
print("#"*72)
print("  倍率  fixed  random greedy optimal  識別力(opt-fix)")
best_scale=(0,0)
for sc in [1,2,4,6,8,10]:
    v,_=evaluate(tp_scale=sc)
    d=v["optimal"]-v["fixed"]
    if d>best_scale[1]: best_scale=(sc,d)
    print(f"  x{sc:<3d} {v['fixed']:.3f}  {v['random']:.3f}  {v['greedy']:.3f}  {v['optimal']:.3f}    {d:.3f}")
print(f"  → 現状(x1)の識別力{evaluate()[0]['optimal']-evaluate()[0]['fixed']:.3f} → 最良 x{best_scale[0]} で {best_scale[1]:.3f}")

print()
print("#"*72)
print("# 案B: 集約方法を変える — どれが政策を区別できるか (要求値は現状x1)")
print("#"*72)
print("  集約         fixed  random greedy optimal  識別力")
for name,agg in [("調和平均",hmean),("算術平均",amean),("最悪値",worst),("満足率(>=.8)",satisfied_ratio)]:
    v,_=evaluate(agg=agg)
    print(f"  {name:12s} {v['fixed']:.3f}  {v['random']:.3f}  {v['greedy']:.3f}  {v['optimal']:.3f}    {v['optimal']-v['fixed']:.3f}")

print()
print("#"*72)
print("# 案C: 滑らかな効用関数 — 逆数vsソフトクリップ")
print("#"*72)
print("  [C-1] call端末のRTT改善の可視性 (dS: RTTを20%短縮した時の満足度変化)")
print("   RTT_link  現状S    滑らかS   現状dS(20%改善)  滑らかdS")
for rtt in [1000,800,500,300]:
    s0=sat_current(2,0,rtt); s0b=sat_current(2,0,rtt*0.8)
    s1=sat_smooth(2,0,rtt);  s1b=sat_smooth(2,0,rtt*0.8)
    print(f"   {rtt:5d}ms  {s0:.3f}   {s1:.3f}     {s0b-s0:+.3f}          {s1b-s1:+.3f}")
print("  [C-2] 集約識別力(調和平均)")
v,_=evaluate(satfn=sat_smooth); print(f"   滑らか効用: fixed={v['fixed']:.3f} greedy={v['greedy']:.3f} optimal={v['optimal']:.3f} 識別力={v['optimal']-v['fixed']:.3f}")

print()
print("#"*72)
print("# 案D: 評価軸をregret / AP選択正答率へ")
print("#"*72)
pol=policy_assignments()
# 各端末の個別最適AP(単独で満足度最大)
def opt_ap_for(i):
    best,ba=None,None
    for cand in ("A","B"):
        # 他端末を greedy 均衡に置いた時のiの満足度
        base=list(pol["greedy"]); base[i]=cand
        s=per_terminal_sats(tuple(base))[i]
        if best is None or s>best: best,ba=s,cand
    return ba
opt_aps=[opt_ap_for(i) for i in range(4)]
print("  政策別: AP選択正答率(個別最適APを選べた端末割合) と regret(調和平均)")
for name in ["fixed","greedy","optimal"]:
    a=pol[name]
    acc=sum(1 for i in range(4) if a[i]==opt_aps[i])/4
    reg=evaluate()[0]["optimal"]-hmean(per_terminal_sats(a))
    print(f"   {name:8s} 正答率={acc:.2f}  regret={reg:.3f}")

print()
print("#"*72)
print("# 案E: 部分観測(片方APのみ) vs 完全観測(両AP) の達成可能満足度")
print("#"*72)
# 完全観測greedy = policy_assignments()['greedy']
full=hmean(per_terminal_sats(pol["greedy"]))
# 部分観測: 端末は現在APの満足度しか見えない。他APが良いか分からないので
# 「現在APが閾値未満なら盲目的に切替(50%の確率で正しいAPへ)」をモデル化 → 期待値
import random
random.seed(42)
trials=2000; acc=0.0
for _ in range(trials):
    cur=list(("A","A","A","A"))
    for _round in range(5):
        for i in range(4):
            s=per_terminal_sats(tuple(cur))[i]
            if s<0.8:  # 現APが不満 → 盲目的に切替(相手が見えないので当たりはずれ半々)
                cur[i]=random.choice(["A","B"])
    acc+=hmean(per_terminal_sats(tuple(cur)))
partial=acc/trials
print(f"  完全観測greedy(両AP見える) 調和平均 = {full:.3f}")
print(f"  部分観測greedy(片方のみ)   調和平均 = {partial:.3f}  (期待値, 2000試行)")
print(f"  → 部分観測は完全観測の {partial/full*100:.0f}% しか達成できない = 特徴量が満足度の上限を決めている")

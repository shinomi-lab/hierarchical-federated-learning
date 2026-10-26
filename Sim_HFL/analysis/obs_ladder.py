#!/usr/bin/env python3
"""観測性ラダー: 特徴量が満足度に課す天井を、時間変動負荷のもとで測る。
実機式(virtual_congestion / TerminalSatisfaction)を使用。

観測性レベル(=特徴量設計):
  L0 要求のみ    : モデル入力=アプリ要求(定数)。=現状シミュ。混雑を見ずアプリ種別で静的にAP決定。
  L1 現在AP実測  : 入力=現在APの[tp,rtt]。=現状実機。自APの劣化は分かるが相手APは見えない。
  L2 両AP実測    : 入力=両APの[tp,rtt]。=提案の修正。両方見て最良を選べる。

時間変動負荷: 各APに背景ユーザ b_A(t), b_B(t) がラウンド毎に変動 → 最適APが時間で変わる。
静的マッピング(L0)はこれに追随できず、観測性(L1/L2)が効くはず。
"""
import itertools, random, statistics as st

AP = {"A": dict(rtt0=20.0, tp0=50.0, mu=2.0, n=2),
      "B": dict(rtt0=80.0, tp0=20.0, mu=5.0, n=10)}
def eb(n,A):
    if n<=0: return 1.0
    if A<=0: return 0.0
    B=1.0
    for k in range(1,n+1): B=(A*B)/(k+A*B)
    return B
def link(ap, lam):
    p=AP[ap]
    rtt = 10000.0 if lam>=p["mu"] else (1.0/(p["mu"]-lam))*1000.0
    rtt = min(10000.0, p["rtt0"]+max(0.0,rtt-p["rtt0"]))
    tp = max(0.0, p["tp0"]*(1.0-eb(p["n"], lam/p["mu"])))
    return rtt, tp
RTT_PRIOR={2,3}; TPN=[5.,10.,0.,0.]; RTN=[0.,0.,200.,100.]; EPS=1e-6  # 2026-08-19 配信/RTT整合
def sat(i,tp,rtt):
    raw = RTN[i]/max(rtt,EPS) if i in RTT_PRIOR else tp/max(TPN[i],EPS)
    return min(1.0,max(EPS,raw))
def hm(xs): return len(xs)/sum(1.0/max(x,EPS) for x in xs)

N=4  # 4端末 apps=[browser,video,call,other]

def ap_states(choices, bg):
    """全端末の割当choices(list of 'A'/'B')と背景負荷bg={'A':,'B':}から各端末満足度"""
    nA=sum(1 for c in choices if c=='A'); nB=N-nA
    lamA=nA+bg['A']; lamB=nB+bg['B']
    sats=[]
    for i,c in enumerate(choices):
        rtt,tp = link(c, lamA if c=='A' else lamB)
        sats.append(sat(i,tp,rtt))
    return sats, (lamA,lamB)

def best_static_mapping(bg_dist_samples):
    """L0: 背景の平均的分布で最良の静的アプリ→AP割当を全探索(2^4)"""
    best=None; bestv=-1
    for assign in itertools.product('AB',repeat=N):
        vals=[]
        for bg in bg_dist_samples:
            s,_=ap_states(list(assign),bg)
            vals.append(hm(s))
        m=st.mean(vals)
        if m>bestv: bestv,best=m,assign
    return list(best)

def bg_sequence(seed, T, bg_max, dwell=1):
    """背景負荷系列。dwell=保持ラウンド数(自己相関)。dwell=1でi.i.d.。
    run/run_oracle が同一系列を見るよう seed で決定論化。"""
    r=random.Random(seed+1000); seq=[]; curbg={'A':r.randint(0,bg_max),'B':r.randint(0,bg_max)}
    for t in range(T):
        if t>0 and t%dwell==0:
            curbg={'A':r.randint(0,bg_max),'B':r.randint(0,bg_max)}
        seq.append(dict(curbg))
    return seq

def run(seed, T=60, level="L2", bg_max=2, dwell=1):
    rnd=random.Random(seed)
    # 背景負荷サンプル(静的マッピング学習用)
    bg_samples=[{'A':rnd.randint(0,bg_max),'B':rnd.randint(0,bg_max)} for _ in range(200)]
    static_map = best_static_mapping(bg_samples)
    seq=bg_sequence(seed,T,bg_max,dwell)

    cur=['A']*N                 # 現在の割当
    prev_obs=None               # 前ラウンドのAP状態観測
    prev_sat=[1.0]*N            # 観測初期値(未観測時の事前)
    prev_lam=(float(N),0.0)     # 初期は全員AP_A想定
    hist=[]; switches=0
    for t in range(T):
        bg=seq[t]
        # ── 各端末が観測性levelに応じて次の割当を決める ──
        newcur=cur[:]
        if level=="L0":
            newcur=static_map[:]            # 静的(混雑を見ない)
        else:
            # 前ラウンドのリンク状態を観測に使う(realistic: 測ってから決める)
            for i in range(N):
                if level=="L1":
                    # 自APの満足度のみ既知。閾値未満なら相手へ移る(相手は見えない=賭け)
                    s_here = prev_sat[i] if prev_obs else 1.0
                    if s_here < 0.8:
                        newcur[i] = 'B' if cur[i]=='A' else 'A'  # 盲目的スイッチ
                    else:
                        newcur[i] = cur[i]
                elif level=="L2":
                    # 両AP観測: 各候補APに自分が入った場合の満足度を前ラウンド負荷で見積り最良を選ぶ
                    best_ap,best_s=cur[i],-1
                    for cand in ('A','B'):
                        # 前ラウンドのそのAPの λ を使って自分の満足度を予測
                        lam = prev_lam[0] if cand=='A' else prev_lam[1]
                        # 自分が今そのAPにいなければ+1
                        if cur[i]!=cand: lam+=1
                        rtt,tp=link(cand,lam)
                        sc=sat(i,tp,rtt)
                        if sc>best_s: best_s,best_ap=sc,cand
                    newcur[i]=best_ap
        switches += sum(1 for i in range(N) if newcur[i]!=cur[i])
        cur=newcur
        sats,lam = ap_states(cur,bg)
        hist.append(hm(sats))
        prev_obs=True; prev_sat=sats; prev_lam=lam
    return st.mean(hist), switches

print("時間変動負荷(背景ユーザ0-2/AP)のもとでの観測性ラダー")
print("各レベル20シード平均の 時間平均満足度(調和平均) と 総切替回数")
print()
print(f"  {'レベル(特徴量)':22s} {'満足度(HM)':>12s} {'切替回数':>10s}")
results={}
for level,label in [("L0","L0 要求のみ(現状sim)"),("L1","L1 現在AP実測(現状実機)"),("L2","L2 両AP実測(提案)")]:
    hms=[];sws=[]
    for seed in range(20):
        h,s=run(seed,level=level); hms.append(h); sws.append(s)
    results[level]=st.mean(hms)
    print(f"  {label:22s} {st.mean(hms):>12.3f} {st.mean(sws):>10.1f}")

# 参照: 毎ラウンドのオラクル(全知・全探索)
def run_oracle(seed,T=60,bg_max=2,dwell=1):
    seq=bg_sequence(seed,T,bg_max,dwell); hist=[]
    for bg in seq:
        best=max((hm(ap_states(list(a),bg)[0]) for a in itertools.product('AB',repeat=N)))
        hist.append(best)
    return st.mean(hist)
orc=st.mean([run_oracle(s) for s in range(20)])
print(f"  {'(参照)毎ラウンドoracle':22s} {orc:>12.3f}")
print()
print("── 両AP特徴量(L1→L2)の効果 ──")
print(f"  L0→L1 (要求のみ→現在AP実測) : {results['L1']-results['L0']:+.3f}")
print(f"  L1→L2 (現在AP実測→両AP実測) : {results['L2']-results['L1']:+.3f}  ← 提案の効果")
print(f"  L0→L2 (総改善)              : {results['L2']-results['L0']:+.3f}")
print(f"  oracleとの残差(L2)          : {orc-results['L2']:+.3f}")

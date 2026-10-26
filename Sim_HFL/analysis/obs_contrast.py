#!/usr/bin/env python3
"""対比実験: 動的AP選択が"効く条件"を特定する。
軸1: APがタイプ分化(現状) vs 同質(A=B同スペック)
軸2: 観測の鮮度 — 静的 / 遅延観測greedy / 遅延+クールダウン / その場観測greedy(理想)
狙い:
 - タイプ分化では静的≈oracle(動的の余地なし=限界)を再確認
 - 同質+自己相関負荷では動的が静的を上回るか(=条件付き有効)
 - 遅延greedyは振動、クールダウンで安定、その場greedyは上限 → HFL静的の"安定性の価値"を示す
実機式(satisfaction/M/M1/ErlangB)使用。
"""
import itertools, random, statistics as st

def eb(n,A):
    if n<=0: return 1.0
    if A<=0: return 0.0
    B=1.0
    for k in range(1,n+1): B=(A*B)/(k+A*B)
    return B
def link(cfg, lam):
    rtt = 10000.0 if lam>=cfg["mu"] else (1.0/(cfg["mu"]-lam))*1000.0
    rtt = min(10000.0, cfg["rtt0"]+max(0.0,rtt-cfg["rtt0"]))
    tp = max(0.0, cfg["tp0"]*(1.0-eb(cfg["n"], lam/cfg["mu"])))
    return rtt, tp
RTT_PRIOR={2,3}; TPN=[5.,10.,0.,0.]; RTN=[0.,0.,200.,100.]; EPS=1e-6  # 2026-08-19 配信/RTT整合
def sat(app,tp,rtt):
    raw = RTN[app]/max(rtt,EPS) if app in RTT_PRIOR else tp/max(TPN[app],EPS)
    return min(1.0,max(EPS,raw))
def hm(xs): return len(xs)/sum(1.0/max(x,EPS) for x in xs)

TYPE_DIFF = {"A":{"rtt0":20.,"tp0":50.,"mu":2.,"n":2}, "B":{"rtt0":80.,"tp0":20.,"mu":5.,"n":10}}
# 同質: A=B。中庸スペック。負荷で混雑するようmu控えめ
HOMOG     = {"A":{"rtt0":40.,"tp0":35.,"mu":4.,"n":4}, "B":{"rtt0":40.,"tp0":35.,"mu":4.,"n":4}}

def sats_of(assign, apps, APc, bg):
    nA=sum(1 for x in assign if x=='A'); nB=len(assign)-nA
    lamA=nA+bg['A']; lamB=nB+bg['B']
    rA=link(APc['A'],lamA); rB=link(APc['B'],lamB)
    out=[]
    for i,x in enumerate(assign):
        r = rA if x=='A' else rB
        out.append(sat(apps[i], r[1], r[0]))
    return out, (lamA,lamB)

def bg_episodic(seed,T,hi,dwell):
    """自己相関・非対称: dwellごとに片方APに高背景hi、他方0(混雑エピソード)。"""
    r=random.Random(seed+7); seq=[]; who=r.choice(['A','B'])
    for t in range(T):
        if t>0 and t%dwell==0: who=r.choice(['A','B','none'])
        if who=='A': seq.append({'A':hi,'B':0})
        elif who=='B': seq.append({'A':0,'B':hi})
        else: seq.append({'A':0,'B':0})
    return seq

def best_static(apps,APc,bg_samples):
    N=len(apps); best=None; bv=-1
    # 端末数少なので全列挙(N<=8想定)
    for assign in itertools.product('AB',repeat=N):
        m=st.mean(hm(sats_of(list(assign),apps,APc,bg)[0]) for bg in bg_samples)
        if m>bv: bv,best=m,list(assign)
    return best

def run(seed,N,APc,mode,T=80,hi=3,dwell=8,cooldown=0):
    apps=[i%4 for i in range(N)]
    rnd=random.Random(seed)
    bg_samples=bg_episodic(seed,40,hi,dwell)
    static=best_static(apps,APc,bg_samples)
    seq=bg_episodic(seed,T,hi,dwell)
    cur=static[:] if mode!='L1' else ['A']*N
    prev_lam=(float(N),0.0); prev_sat=[1.0]*N; prev_obs=False
    last_sw=[-999]*N
    hist=[]; sw=0
    for t,bg in enumerate(seq):
        new=cur[:]
        if mode=='static':
            new=static[:]
        else:
            for i in range(N):
                if cooldown>0 and (t-last_sw[i])<cooldown:  # クールダウン中は動かない
                    continue
                a=apps[i]
                if mode=='L1':
                    s=prev_sat[i] if prev_obs else 1.0
                    if s<0.8: new[i]='B' if cur[i]=='A' else 'A'
                elif mode in ('greedy_stale','greedy_cool'):
                    # 前ラウンドλで両AP評価(遅延観測)
                    best_ap,bs=cur[i],-1
                    for cand in ('A','B'):
                        lam=prev_lam[0] if cand=='A' else prev_lam[1]
                        if cur[i]!=cand: lam+=1
                        r=link(APc[cand],lam); sc=sat(a,r[1],r[0])
                        if sc>bs: bs,best_ap=sc,cand
                    new[i]=best_ap
                elif mode=='greedy_fresh':
                    # その場のλ(現ラウンドbg+現配置)で評価=理想greedy上限
                    nA=sum(1 for x in cur if x=='A'); nB=N-nA
                    best_ap,bs=cur[i],-1
                    for cand in ('A','B'):
                        lam=(nA+bg['A']) if cand=='A' else (nB+bg['B'])
                        if cur[i]!=cand: lam+=1
                        r=link(APc[cand],lam); sc=sat(a,r[1],r[0])
                        if sc>bs: bs,best_ap=sc,cand
                    new[i]=best_ap
        for i in range(N):
            if new[i]!=cur[i]: sw+=1; last_sw[i]=t
        cur=new
        s,lam=sats_of(cur,apps,APc,bg)
        hist.append(hm(s)); prev_sat=s; prev_lam=lam; prev_obs=True
    return st.mean(hist), sw/T

def oracle(seed,N,APc,T=80,hi=3,dwell=8):
    apps=[i%4 for i in range(N)]
    tot=[]
    for bg in bg_episodic(seed,T,hi,dwell):
        best=max(hm(sats_of(list(a),apps,APc,bg)[0]) for a in itertools.product('AB',repeat=N))
        tot.append(best)
    return st.mean(tot)

if __name__=="__main__":
    N=8; SEEDS=range(15)
    for label,APc in [("① タイプ分化AP(現状)",TYPE_DIFF),("② 同質AP(A=B)",HOMOG)]:
        print("="*66); print(f"{label}  (N={N}, 自己相関・非対称負荷)"); print("="*66)
        print(f"  {'方式':26s} {'満足度HM':>9s} {'切替/R':>8s}")
        rows=[("static","静的(=HFLが学習)"),
              ("L1","L1 現在APのみ(現状実機)"),
              ("greedy_stale","両AP・遅延観測(現実greedy)"),
              ("greedy_cool","両AP・遅延+クールダウン5"),
              ("greedy_fresh","両AP・その場観測(理想上限)")]
        res={}
        for mode,name in rows:
            cd=5 if mode=='greedy_cool' else 0
            r=[run(s,N,APc,mode,cooldown=cd) for s in SEEDS]
            hmv=st.mean([x[0] for x in r]); swv=st.mean([x[1] for x in r]); res[mode]=hmv
            print(f"  {name:26s} {hmv:>9.3f} {swv:>8.2f}")
        orc=st.mean([oracle(s,N,APc) for s in SEEDS])
        print(f"  {'(参照)毎ラウンドoracle':26s} {orc:>9.3f}")
        print(f"  → 動的の正味効果(遅延+CD − 静的): {res['greedy_cool']-res['static']:+.3f}")
        print(f"  → 静的とoracleの差(動的の余地)   : {orc-res['static']:+.3f}")
        print()

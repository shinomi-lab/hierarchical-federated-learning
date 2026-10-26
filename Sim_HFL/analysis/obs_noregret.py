#!/usr/bin/env python3
"""B: no-regret学習 vs 貪欲 の検証 (H7: 決定則 > 観測性)。
配信/RTT定義(call+配信の2遅延端末が競合)。実機式使用。

比較:
  静的(=HFL学習相当)
  greedy_stale(L2両AP遅延観測・貪欲)         ← 従来
  noregret_bandit(L1相当: 自APのみ観測・指数重みExp3) ← H7本命
  noregret_full  (L2相当: 両AP観測・Hedge)
仮説H7: 同じ部分観測でも貪欲は振動、no-regretは収束 → noregret_bandit > greedy。
"""
import itertools, random, math, statistics as st

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
RTT_PRIOR={2,3}; TPN=[5.,10.,0.,0.]; RTN=[0.,0.,200.,100.]; EPS=1e-6  # 配信/RTT定義
def sat(app,tp,rtt):
    raw = RTN[app]/max(rtt,EPS) if app in RTT_PRIOR else tp/max(TPN[app],EPS)
    return min(1.0,max(EPS,raw))
def hm(xs): return len(xs)/sum(1.0/max(x,EPS) for x in xs)

TYPE_DIFF={"A":{"rtt0":20.,"tp0":50.,"mu":2.,"n":2},"B":{"rtt0":80.,"tp0":20.,"mu":5.,"n":10}}
HOMOG    ={"A":{"rtt0":40.,"tp0":35.,"mu":4.,"n":4},"B":{"rtt0":40.,"tp0":35.,"mu":4.,"n":4}}

def sats_of(assign, apps, APc, bg):
    nA=sum(1 for x in assign if x=='A'); nB=len(assign)-nA
    lamA=nA+bg['A']; lamB=nB+bg['B']
    rA=link(APc['A'],lamA); rB=link(APc['B'],lamB)
    out=[sat(apps[i], (rA if x=='A' else rB)[1], (rA if x=='A' else rB)[0]) for i,x in enumerate(assign)]
    return out,(lamA,lamB)

def bg_episodic(seed,T,hi,dwell):
    r=random.Random(seed+7); seq=[]; who=r.choice(['A','B'])
    for t in range(T):
        if t>0 and t%dwell==0: who=r.choice(['A','B','none'])
        seq.append({'A':hi,'B':0} if who=='A' else ({'A':0,'B':hi} if who=='B' else {'A':0,'B':0}))
    return seq

def best_static(apps,APc,bg_samples):
    N=len(apps); best=None; bv=-1
    for a in itertools.product('AB',repeat=N):
        m=st.mean(hm(sats_of(list(a),apps,APc,bg)[0]) for bg in bg_samples)
        if m>bv: bv,best=m,list(a)
    return best

def run(seed,N,APc,mode,T=80,hi=3,dwell=8,cooldown=0,eta=0.3,gamma=0.08):
    apps=[i%4 for i in range(N)]
    bg_samples=bg_episodic(seed,40,hi,dwell)
    static=best_static(apps,APc,bg_samples)
    seq=bg_episodic(seed,T,hi,dwell)
    rng=random.Random(seed+555)
    cur=static[:] if mode in('static','greedy_stale','greedy_cool') else ['A']*N
    w=[[1.0,1.0] for _ in range(N)]     # no-regret 重み
    prev_lam=(float(N),0.0); prev_sat=[1.0]*N; prev_obs=False
    last_sw=[-999]*N; hist=[]; sw=0
    for t,bg in enumerate(seq):
        new=cur[:]; probs=[None]*N
        if mode=='static':
            new=static[:]
        elif mode in('greedy_stale','greedy_cool'):
            for i in range(N):
                if cooldown>0 and (t-last_sw[i])<cooldown: continue
                a=apps[i]; best_ap,bs=cur[i],-1
                for cand in('A','B'):
                    lam=prev_lam[0] if cand=='A' else prev_lam[1]
                    if cur[i]!=cand: lam+=1
                    r=link(APc[cand],lam); sc=sat(a,r[1],r[0])
                    if sc>bs: bs,best_ap=sc,cand
                new[i]=best_ap
        elif mode in('noregret_bandit','noregret_social'):
            # L1相当: 自APのみ観測。Exp3で確率的に選択
            for i in range(N):
                wa,wb=w[i]; s=wa+wb
                pa=(1-gamma)*wa/s+gamma*0.5; probs[i]=[pa,1-pa]
                new[i]='A' if rng.random()<pa else 'B'
        elif mode=='noregret_full':
            # L2相当: 両APの見込み報酬(前ラウンドλ)でHedge更新→確率選択
            for i in range(N):
                a=apps[i]; est=[]
                for cand in('A','B'):
                    lam=prev_lam[0] if cand=='A' else prev_lam[1]
                    if cur[i]!=cand: lam+=1
                    r=link(APc[cand],lam); est.append(sat(a,r[1],r[0]))
                w[i][0]*=math.exp(eta*est[0]); w[i][1]*=math.exp(eta*est[1])
                mx=max(w[i]); w[i][0]/=mx; w[i][1]/=mx
                s=w[i][0]+w[i][1]; pa=w[i][0]/s; probs[i]=[pa,1-pa]
                new[i]='A' if rng.random()<pa else 'B'
        for i in range(N):
            if new[i]!=cur[i]: sw+=1; last_sw[i]=t
        cur=new
        sats,lam=sats_of(cur,apps,APc,bg); hist.append(hm(sats))
        # bandit の重み更新(観測=自APの満足度 or 社会報酬=全体HM)
        if mode in('noregret_bandit','noregret_social'):
            shared=hm(sats)   # 社会報酬(協調): 全端末が系のHMを共有
            for i in range(N):
                ai=0 if cur[i]=='A' else 1
                r = shared if mode=='noregret_social' else sats[i]
                pa=probs[i][ai]; rhat=r/max(pa,1e-3)
                w[i][ai]*=math.exp(gamma*rhat/2.0)
                mx=max(w[i]); w[i][0]/=mx; w[i][1]/=mx
        prev_sat=sats; prev_lam=lam; prev_obs=True
    return st.mean(hist), sw/T

if __name__=="__main__":
    N=8; SEEDS=range(15)
    for label,APc in [("① タイプ分化AP",TYPE_DIFF),("② 同質AP",HOMOG)]:
        print("="*60); print(f"{label} (N={N}, 配信/RTT定義, 2遅延端末競合)"); print("="*60)
        print(f"  {'方式':30s} {'満足度HM':>9s} {'切替/R':>8s}")
        for mode,name,cd in [("static","静的(=HFL学習)",0),
                             ("greedy_stale","L2両AP・貪欲(遅延)",0),
                             ("greedy_cool","L2両AP・貪欲+CD5",5),
                             ("noregret_bandit","L1自APのみ・no-regret ★H7",0),
                             ("noregret_full","L2両AP・no-regret",0)]:
            r=[run(s,N,APc,mode,cooldown=cd) for s in SEEDS]
            print(f"  {name:30s} {st.mean([x[0] for x in r]):>9.3f} {st.mean([x[1] for x in r]):>8.2f}")
        print()

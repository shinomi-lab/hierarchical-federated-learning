#!/usr/bin/env python3
"""協調(CTDE/H4)の検証: 中央集約による協調割当が天井を破れるか。
新主張: 反応型(同時greedy=herding)も確率型(no-regret)も負ける。上げ筋は"協調"。
配信/RTT定義。実機式。

政策:
  fixed        : 全員AP_A
  static       : 最良の固定割当(=HFL学習相当)
  greedy_sim   : 独立・同時best-response(遅延観測) = herding
  greedy_seq   : 逐次best-response(1端末/ラウンド, 現状態観測) = 協調(直列化≒async)
  central_stale: 中央が前ラウンド状態で最適joint割当を計算(現実的CTDE)
  central_fresh: 中央が現ラウンド状態で最適joint(=協調上限=oracle)
"""
import itertools, random, statistics as st

def eb(n,A):
    if n<=0: return 1.0
    if A<=0: return 0.0
    B=1.0
    for k in range(1,n+1): B=(A*B)/(k+A*B)
    return B
def link(cfg,lam):
    rtt=10000.0 if lam>=cfg["mu"] else (1.0/(cfg["mu"]-lam))*1000.0
    rtt=min(10000.0,cfg["rtt0"]+max(0.0,rtt-cfg["rtt0"]))
    tp=max(0.0,cfg["tp0"]*(1.0-eb(cfg["n"],lam/cfg["mu"])))
    return rtt,tp
RTT_PRIOR={2,3}; TPN=[5.,10.,0.,0.]; RTN=[0.,0.,200.,100.]; EPS=1e-6  # 配信/RTT
def sat(app,tp,rtt):
    raw=RTN[app]/max(rtt,EPS) if app in RTT_PRIOR else tp/max(TPN[app],EPS)
    return min(1.0,max(EPS,raw))
def hm(xs): return len(xs)/sum(1.0/max(x,EPS) for x in xs)

TYPE_DIFF={"A":{"rtt0":20.,"tp0":50.,"mu":2.,"n":2},"B":{"rtt0":80.,"tp0":20.,"mu":5.,"n":10}}
HOMOG    ={"A":{"rtt0":40.,"tp0":35.,"mu":4.,"n":4},"B":{"rtt0":40.,"tp0":35.,"mu":4.,"n":4}}

def sats_of(assign,apps,APc,bg):
    nA=sum(1 for x in assign if x=='A'); nB=len(assign)-nA
    lamA=nA+bg['A']; lamB=nB+bg['B']
    rA=link(APc['A'],lamA); rB=link(APc['B'],lamB)
    return [sat(apps[i],(rA if x=='A' else rB)[1],(rA if x=='A' else rB)[0]) for i,x in enumerate(assign)],(lamA,lamB)

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

def central_best(apps,APc,bg):
    """中央: 全割当を探索しHM最大のjoint割当を返す(count対称で高速化可だが N<=10 は全探索)"""
    N=len(apps); best=None; bv=-1
    for a in itertools.product('AB',repeat=N):
        v=hm(sats_of(list(a),apps,APc,bg)[0])
        if v>bv: bv,best=v,list(a)
    return best

def run(seed,N,APc,mode,T=80,hi=3,dwell=8,coord_every=1):
    apps=[i%4 for i in range(N)]
    bg_samples=bg_episodic(seed,40,hi,dwell)
    static=best_static(apps,APc,bg_samples)
    seq=bg_episodic(seed,T,hi,dwell)
    cur=static[:] if mode!='fixed' else ['A']*N
    if mode=='fixed': cur=['A']*N
    prev_lam=(float(N),0.0); hist=[]; sw=0; rr=0
    last_central=None
    for t,bg in enumerate(seq):
        new=cur[:]
        if mode=='fixed':
            new=['A']*N
        elif mode=='static':
            new=static[:]
        elif mode=='greedy_sim':
            for i in range(N):
                a=apps[i]; best_ap,bs=cur[i],-1
                for cand in('A','B'):
                    lam=prev_lam[0] if cand=='A' else prev_lam[1]
                    if cur[i]!=cand: lam+=1
                    r=link(APc[cand],lam); sc=sat(a,r[1],r[0])
                    if sc>bs: bs,best_ap=sc,cand
                new[i]=best_ap
        elif mode=='greedy_seq':
            # 1端末だけ更新(現状態を反映して直列化=協調/async相当)
            i=rr%N; rr+=1; a=apps[i]; best_ap,bs=cur[i],-1
            for cand in('A','B'):
                trial=cur[:]; trial[i]=cand
                sc=sats_of(trial,apps,APc,bg)[0][i]   # 現ラウンド状態で自分の満足度
                if sc>bs: bs,best_ap=sc,cand
            new[i]=best_ap
        elif mode in('central_stale','central_fresh'):
            if t%coord_every==0:
                use_bg = bg if mode=='central_fresh' else (seq[t-1] if t>0 else bg)
                last_central=central_best(apps,APc,use_bg)
            new=last_central[:] if last_central else static[:]
        for i in range(N):
            if new[i]!=cur[i]: sw+=1
        cur=new
        sats,lam=sats_of(cur,apps,APc,bg); hist.append(hm(sats)); prev_lam=lam
    return st.mean(hist), sw/T

def oracle(seed,N,APc,T=80,hi=3,dwell=8):
    apps=[i%4 for i in range(N)]
    return st.mean(hm(sats_of(central_best(apps,APc,bg),apps,APc,bg)[0]) for bg in bg_episodic(seed,T,hi,dwell))

if __name__=="__main__":
    N=8; SEEDS=range(15)
    for label,APc in [("① タイプ分化AP",TYPE_DIFF),("② 同質AP",HOMOG)]:
        print("="*62); print(f"{label} (N={N}, 配信/RTT, 2遅延端末競合)"); print("="*62)
        print(f"  {'政策':34s} {'満足度HM':>9s} {'切替/R':>8s}")
        for mode,name in [("fixed","fixed"),("static","静的(=HFL)"),
                          ("greedy_sim","独立・同時greedy(herding)"),
                          ("greedy_seq","逐次greedy(協調/async相当)★"),
                          ("central_stale","中央協調・遅延観測★"),
                          ("central_fresh","中央協調・その場(=協調上限)")]:
            r=[run(s,N,APc,mode) for s in SEEDS]
            print(f"  {name:34s} {st.mean([x[0] for x in r]):>9.3f} {st.mean([x[1] for x in r]):>8.2f}")
        print(f"  {'(参照)oracle':34s} {st.mean([oracle(s,N,APc) for s in SEEDS]):>9.3f}")
        print()

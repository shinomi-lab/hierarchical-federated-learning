#!/usr/bin/env python3
"""観測性ラダーのN-スケール版: 端末数を増やして競合を強めた時、
静的マッピング(L0)の優位が崩れる=動的観測(L2)が効き始めるかを検証する。

高速化: 同一アプリ端末は交換可能なので、割当をcount空間 (各アプリ何台がAP_Aか)
で厳密列挙する。oracle/静的探索が 2^N → Π(c_a+1) (~数百) に激減。
実機式(virtual_congestion / TerminalSatisfaction)使用。
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
def sat(app,tp,rtt):
    raw = RTN[app]/max(rtt,EPS) if app in RTT_PRIOR else tp/max(TPN[app],EPS)
    return min(1.0,max(EPS,raw))
def hm(xs): return len(xs)/sum(1.0/max(x,EPS) for x in xs)

def counts_for(N):
    """アプリ0..3を巡回割当。各アプリの台数 c_a を返す。"""
    apps=[i%4 for i in range(N)]
    c=[apps.count(a) for a in range(4)]
    return apps, c

def hm_from_kvec(kvec, c, bg):
    """kvec[a]=アプリaのうちAP_Aに置く台数。全端末のHMを返す。"""
    nA=sum(kvec); nB=sum(c)-nA
    lamA=nA+bg['A']; lamB=nB+bg['B']
    rA={a:link('A',lamA) for a in range(4)}; rB={a:link('B',lamB) for a in range(4)}
    sats=[]
    for a in range(4):
        for _ in range(kvec[a]):      sats.append(sat(a,rA[a][1],rA[a][0]))
        for _ in range(c[a]-kvec[a]): sats.append(sat(a,rB[a][1],rB[a][0]))
    return hm(sats)

def all_kvecs(c):
    for kv in itertools.product(*[range(ca+1) for ca in c]):
        yield kv

def best_static(c, bg_samples):
    best=None; bestv=-1
    for kv in all_kvecs(c):
        m=st.mean(hm_from_kvec(kv,c,bg) for bg in bg_samples)
        if m>bestv: bestv,best=m,kv
    return best

def oracle_round(c, bg):
    return max(hm_from_kvec(kv,c,bg) for kv in all_kvecs(c))

def bg_seq(seed,T,bg_max,dwell):
    r=random.Random(seed+1000); seq=[]; cb={'A':r.randint(0,bg_max),'B':r.randint(0,bg_max)}
    for t in range(T):
        if t>0 and t%dwell==0: cb={'A':r.randint(0,bg_max),'B':r.randint(0,bg_max)}
        seq.append(dict(cb))
    return seq

def kvec_to_assign(kv,c,apps):
    """count kv を端末別割当(list of 'A'/'B')へ。各アプリの先頭k台をA。"""
    assign=[None]*len(apps); placed={a:0 for a in range(4)}
    for i,a in enumerate(apps):
        if placed[a] < kv[a]: assign[i]='A'; placed[a]+=1
        else: assign[i]='B'
    return assign

def run(seed, N, level, T=60, bg_max=2, dwell=5):
    apps,c = counts_for(N)
    rnd=random.Random(seed)
    bg_samples=[{'A':rnd.randint(0,bg_max),'B':rnd.randint(0,bg_max)} for _ in range(60)]
    static_kv=best_static(c,bg_samples)
    static_assign=kvec_to_assign(static_kv,c,apps)
    seq=bg_seq(seed,T,bg_max,dwell)

    cur=['A']*N; prev_sat=[1.0]*N; prev_lam=(float(N),0.0); prev_obs=False
    hist=[]; switches=0
    for bg in seq:
        newcur=cur[:]
        if level=="L0":
            newcur=static_assign[:]
        else:
            for i in range(N):
                a=apps[i]
                if level=="L1":
                    s_here=prev_sat[i] if prev_obs else 1.0
                    newcur[i]=('B' if cur[i]=='A' else 'A') if s_here<0.8 else cur[i]
                else: # L2 両AP: 前ラウンドλで自分の各AP満足度を予測し最良
                    best_ap,best_s=cur[i],-1
                    for cand in ('A','B'):
                        lam=prev_lam[0] if cand=='A' else prev_lam[1]
                        if cur[i]!=cand: lam+=1
                        rtt,tp=link(cand,lam)
                        sc=sat(a,tp,rtt)
                        if sc>best_s: best_s,best_ap=sc,cand
                    newcur[i]=best_ap
        switches+=sum(1 for i in range(N) if newcur[i]!=cur[i]); cur=newcur
        nA=sum(1 for x in cur if x=='A'); nB=N-nA
        lamA=nA+bg['A']; lamB=nB+bg['B']
        sats=[]
        for i,x in enumerate(cur):
            rtt,tp=link(x, lamA if x=='A' else lamB); sats.append(sat(apps[i],tp,rtt))
        hist.append(hm(sats)); prev_sat=sats; prev_lam=(lamA,lamB); prev_obs=True
    return st.mean(hist), switches/T

def run_oracle(seed,N,T=60,bg_max=2,dwell=5):
    apps,c=counts_for(N)
    return st.mean(oracle_round(c,bg) for bg in bg_seq(seed,T,bg_max,dwell))

if __name__=="__main__":
    print("N-スケール: 端末数を増やし競合を強めた時、静的(L0)優位が崩れ両AP(L2)が効くか")
    print("bg_max=2, dwell=5, 15seed平均。切替/端末/ラウンド も表示(振動指標)")
    print(f"  {'N':>3} | {'L0静的':>7} {'L1現在':>7} {'L2両AP':>7} {'oracle':>7} | {'L2-L0':>6} | {'L1切替':>6} {'L2切替':>6}")
    for N in [4,8,12,16,20,24]:
        L0=st.mean([run(s,N,'L0')[0] for s in range(15)])
        r1=[run(s,N,'L1') for s in range(15)]; L1=st.mean([x[0] for x in r1]); sw1=st.mean([x[1] for x in r1])
        r2=[run(s,N,'L2') for s in range(15)]; L2=st.mean([x[0] for x in r2]); sw2=st.mean([x[1] for x in r2])
        orc=st.mean([run_oracle(s,N) for s in range(15)])
        print(f"  {N:>3} | {L0:>7.3f} {L1:>7.3f} {L2:>7.3f} {orc:>7.3f} | {L2-L0:>+6.3f} | {sw1:>6.2f} {sw2:>6.2f}")

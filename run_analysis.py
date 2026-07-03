#!/usr/bin/env python3
"""
Full re-run of all three analyses on refreshed data (through 2026-06-29):
  1. Per-asset Monte Carlo (GJR-GARCH-t vol + Merton jumps + Bayesian drift
     shrinkage) over 5/10/20/30y horizons  -> mc_results.csv
  2. A 30-year monthly-DCA portfolio comparison
  3. A 10-year lump-sum portfolio (5k now + 5k Jan) 60/25/15, rebalanced annually
"""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from pathlib import Path
from arch import arch_model

BASE = Path(__file__).parent
np.random.seed(7)

NAMES = {
 'VWCE.DE':'VWCE All-World','CSPX.L':'S&P 500 UCITS','IWDA.L':'MSCI World',
 'EIMI.L':'EM IMI','IFSW.L':'MSCI Quality','IWMO.L':'MSCI Momentum',
 'WSML.L':'MSCI Small Cap','IWVL.L':'MSCI Value','QQQ':'Nasdaq-100',
 'VGT':'Vanguard IT','AAPL':'Apple','MSFT':'Microsoft','NVDA':'NVIDIA',
 'GOOGL':'Alphabet','AMZN':'Amazon','TLT':'20yr Treasury','GLD':'Gold',
 'VNQ':'US REITs','SSO':'S&P500 2x Lev','QLD':'QQQ 2x Lev','BRK-B':'Berkshire B',
 'VT':'VT Total World','ACWI':'iShares ACWI'}

px = pd.read_csv(BASE/"universe_prices.csv", index_col=0, parse_dates=True).sort_index()
print(f"Data: {px.shape[1]} assets, {px.index.min().date()} -> {px.index.max().date()}\n")

N_SIM = 4000
HORIZONS = {'5y':60, '10y':120, '20y':240, '30y':360}
# Bayesian drift shrinkage: pull each asset's sample mean toward a market prior.
MARKET_PRIOR_ANN = 0.07      # long-run real-ish equity prior
SHRINK = 0.45                # weight on the prior (0=all data, 1=all prior)

def fit_asset(ret_d):
    """Return monthly params: drift (shrunk), GJR-GARCH-t persistence/vol, jumps."""
    r = ret_d.dropna()*100.0           # daily % returns for arch
    # GJR-GARCH(1,1) with Student-t
    am = arch_model(r, vol='GARCH', p=1, o=1, q=1, dist='t', mean='Constant')
    res = am.fit(disp='off')
    p = res.params
    omega=p['omega']; a=p.get('alpha[1]',0); g=p.get('gamma[1]',0); b=p.get('beta[1]',0)
    nu = p.get('nu', 8.0)
    persist = a + 0.5*g + b
    # long-run daily variance -> annualized vol
    lr_var_d = omega/max(1e-9, 1-persist) / 1e4     # back to decimal
    vol_ann = np.sqrt(lr_var_d*252)
    # Merton jumps: treat daily moves beyond 3.5 sigma as jumps
    rd = ret_d.dropna()
    sd = rd.std()
    jumps = rd[np.abs(rd-rd.mean())>3.5*sd]
    lam_ann = len(jumps)/len(rd)*252                 # jumps per year
    jmu = jumps.mean() if len(jumps)>0 else 0.0
    jsd = jumps.std() if len(jumps)>1 else sd*2
    # Drift: sample annual mean, shrunk to prior
    mu_sample = rd.mean()*252
    mu_post = (1-SHRINK)*mu_sample + SHRINK*MARKET_PRIOR_ANN
    return dict(mu_post=mu_post, vol_ann=vol_ann, persist=persist, nu=nu,
                lam_ann=lam_ann, jmu=jmu, jsd=jsd)

def sim_paths(par, n_months, n_sim):
    """Monthly compounding simulation with t-noise + Merton jumps."""
    mu_m = (1+par['mu_post'])**(1/12)-1
    vol_m = par['vol_ann']/np.sqrt(12)
    nu = max(2.5, par['nu'])
    lam_m = par['lam_ann']/12
    # Student-t standardized to unit variance
    t = np.random.standard_t(nu, size=(n_sim, n_months)) * np.sqrt((nu-2)/nu)
    diffus = mu_m + vol_m*t
    # jumps per month ~ Poisson(lam_m), aggregate jump size ~ Normal
    njump = np.random.poisson(lam_m, size=(n_sim, n_months))
    jump = njump*(par['jmu']) + np.sqrt(np.maximum(njump,0))*par['jsd']*np.random.standard_normal((n_sim,n_months))
    monthly = diffus + jump
    return monthly      # (n_sim, n_months) simple monthly returns

# ---------- 1) PER-ASSET MONTE CARLO ----------
assets = list(px.columns)
rows=[]
print("Per-asset Monte Carlo (GJR-GARCH-t + jumps + Bayesian drift):")
for tk in assets:
    ret = px[tk].pct_change().dropna()
    if len(ret) < 250:
        print(f"  {tk:<9} skip (only {len(ret)} obs)"); continue
    par = fit_asset(ret)
    row={'ticker':tk,'name':NAMES.get(tk,tk),'mu_post_ann':par['mu_post']*100,
         'vol_ann':par['vol_ann']*100,'persist':par['persist'],'nu':par['nu']}
    for hk,hm in HORIZONS.items():
        m = sim_paths(par, hm, N_SIM)
        growth = np.prod(1+m, axis=1)        # terminal multiple of 1 unit lump
        # lump of 10k for readability
        term = growth*10000
        row[f'med_{hk}']  = np.median(term)
        row[f'p5_{hk}']   = np.percentile(term,5)
        row[f'p95_{hk}']  = np.percentile(term,95)
        row[f'xirr_{hk}'] = ((np.median(term)/10000)**(1/(hm/12))-1)*100
        row[f'ploss_{hk}']= (term<10000).mean()*100
    rows.append(row)
    print(f"  {tk:<9} muPost={par['mu_post']*100:5.1f}%  10yXIRR={row['xirr_10y']:5.2f}%  "
          f"P(loss10y)={row['ploss_10y']:4.1f}%  30yXIRR={row['xirr_30y']:5.2f}%")

mc = pd.DataFrame(rows).set_index('ticker')
mc.to_csv(BASE/"mc_results.csv")
print(f"\nSaved mc_results.csv ({len(mc)} assets)\n")
print("="*70)

# ---------- shared: correlated monthly engine for portfolios ----------
def corr_engine(port_assets):
    sub = px[port_assets].dropna()
    monthly = sub.resample('ME').last().pct_change().dropna()
    mu_ann = mc.loc[port_assets,'mu_post_ann'].values/100
    mu_m = (1+mu_ann)**(1/12)-1
    vol_m = monthly.std().values
    corr = monthly.corr().values
    D = np.diag(vol_m)
    cov = D@corr@D
    L = np.linalg.cholesky(cov+1e-12*np.eye(len(port_assets)))
    return mu_m, L, monthly.index.min().date(), monthly.index.max().date()

def sim_portfolio(port_assets, weights, n_months, cashflows, n_sim=8000, rebal=12):
    w=np.array(weights,float); w/=w.sum()
    mu_m, L, d0, d1 = corr_engine(port_assets)
    finals=np.empty(n_sim)
    for s in range(n_sim):
        z=np.random.standard_normal((n_months,len(port_assets)))
        rets=mu_m+z@L.T
        h=np.zeros(len(port_assets))
        for m in range(n_months):
            h*=(1+rets[m])
            if m in cashflows: h+=w*cashflows[m]
            if rebal and m>0 and m%rebal==0:
                h=w*h.sum()
        finals[s]=h.sum()
    inv=sum(cashflows.values())
    return dict(med=np.median(finals),p5=np.percentile(finals,5),
                p95=np.percentile(finals,95),
                ploss=(finals<inv).mean()*100,invested=inv,
                d0=d0,d1=d1)

# ---------- 2) YOUR 30-YEAR MONTHLY DCA ----------
print("\n[2] YOUR 30-YEAR MONTHLY DCA  (€1,000/month × 360 months)\n")
dca_assets=['CSPX.L','QQQ','GLD','IFSW.L','IWMO.L','IWDA.L','EIMI.L','VGT']
cf_dca={m:1000.0 for m in range(360)}
your_cands={
 "Your 40/30/20/5/5":      {'CSPX.L':.40,'QQQ':.30,'GLD':.20,'IFSW.L':.05,'IWMO.L':.05},
 "Simplified 50/30/20":    {'CSPX.L':.50,'QQQ':.30,'GLD':.20},
 "Broad+tech 45/25/15/15": {'CSPX.L':.45,'QQQ':.25,'GLD':.15,'IWDA.L':.15},
 "100% S&P500":            {'CSPX.L':1.0},
 "100% Nasdaq":            {'QQQ':1.0},
}
print(f"{'Portfolio':<26}{'median':>12}{'p5':>11}{'CAGR%':>8}{'P(loss)%':>10}")
print("-"*67)
for nm,wd in your_cands.items():
    pa=[a for a in wd]; wv=[wd[a] for a in pa]
    r=sim_portfolio(pa,wv,360,cf_dca,n_sim=3000)
    cagr=((r['med']/r['invested'])**(1/15.5)-1)*100   # ~15.5y avg money life for 30y monthly
    print(f"{nm:<26}{r['med']:>12,.0f}{r['p5']:>11,.0f}{cagr:>8.2f}{r['ploss']:>10.1f}")
print(f"(€360,000 invested over 360 months. corr sample {r['d0']}->{r['d1']})")

# ---------- 3) 10-YEAR LUMP-SUM PORTFOLIO ----------
print("\n[3] 10-YEAR LUMP-SUM  (€5k now + €5k month 6, annual rebalance)\n")
cf_lump={0:5000.0,6:5000.0}
lump_cands={
 "60/25/15 (current plan)": {'CSPX.L':.60,'QQQ':.25,'GLD':.15},
 "55/20/15/10 +EM":         {'CSPX.L':.55,'QQQ':.20,'GLD':.15,'EIMI.L':.10},
 "65/20/15 aggressive":     {'CSPX.L':.65,'QQQ':.20,'GLD':.15},
 "50/30/20 more tech":      {'CSPX.L':.50,'QQQ':.30,'GLD':.20},
 "70/30 SP/Nasdaq":         {'CSPX.L':.70,'QQQ':.30},
 "100% S&P500":             {'CSPX.L':1.0},
}
print(f"{'Portfolio':<26}{'median':>11}{'p5':>10}{'p95':>11}{'CAGR%':>8}{'P(loss)%':>10}")
print("-"*76)
for nm,wd in lump_cands.items():
    pa=[a for a in wd]; wv=[wd[a] for a in pa]
    r=sim_portfolio(pa,wv,120,cf_lump,n_sim=8000)
    cagr=((r['med']/r['invested'])**(1/9.5)-1)*100
    print(f"{nm:<26}{r['med']:>11,.0f}{r['p5']:>10,.0f}{r['p95']:>11,.0f}{cagr:>8.2f}{r['ploss']:>10.1f}")
print(f"(€10,000 invested. corr sample {r['d0']}->{r['d1']})")
print("\nDONE.")

"""
볼린저 밴드(240,2) 종목 필터 백테스트.

조건: BB상한(240,2) > BB중심(240,2)*1.5
  → SMA240 + 2*STD240 > SMA240 * 1.5
  → STD240/SMA240 > 0.25  (변동성 계수 25% 초과 = 고변동성 종목)

비교 조합:
  A. 기준          (필터없음  + MA7이탈)
  B. BR5+MA7       (BR5단독   + MA7이탈)  ← 이전 최고
  C. BB변동성      (BB종목필터 + MA7이탈)
  D. BR5+BB변동성  (BR5단독   + BB종목필터 + MA7이탈)
  E. 기준+BB변동성 (BB종목필터만, 거시필터 없음)
"""
import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, 'src')

import numpy as np
import pandas as pd
from ssen.strategy.backtest_pullback import (
    load_ohlcv, load_ssen_leader_filter, filter_universe,
    build_bullbear_filter,
    _compute_signals, _run_simulation, _macro_stats,
)

STOP_LOSS = 15.0
MAX_HOLD  = 120

t0 = time.time()

df          = load_ohlcv()
ssen_filter = load_ssen_leader_filter('rank', window=20, rank_top=100)
fdf, codes  = filter_universe(df, ratio=5.0)
br5_filter  = build_bullbear_filter(mode='br5_only')

print("\n  이동평균·신호 계산 중...")
sig_df = _compute_signals(fdf, codes)

# BB(240,2) 조건 만족 비율 출력
bb_upper = sig_df["BB_upper240"]
bb_mid   = sig_df["BB_mid240"]
valid    = bb_mid.notna() & bb_upper.notna()
pct_pass = (bb_upper[valid] > bb_mid[valid] * 1.5).mean() * 100
print(f"  BB(240,2) 조건 충족 행 비율: {pct_pass:.1f}%  (BB상한 > 중심*1.5)")

print("  시뮬레이션 실행:")
results = {}
results["A.기준+MA7"]      = _run_simulation(sig_df, codes, STOP_LOSS, MAX_HOLD,
                                 ssen_filter, None,      "기준",     bb_volatility=False)
results["B.BR5+MA7"]       = _run_simulation(sig_df, codes, STOP_LOSS, MAX_HOLD,
                                 ssen_filter, br5_filter,"BR5",      bb_volatility=False)
results["C.기준+BB변동성"] = _run_simulation(sig_df, codes, STOP_LOSS, MAX_HOLD,
                                 ssen_filter, None,      "BB변동성", bb_volatility=True)
results["D.BR5+BB변동성"]  = _run_simulation(sig_df, codes, STOP_LOSS, MAX_HOLD,
                                 ssen_filter, br5_filter,"BR5+BB",   bb_volatility=True)

# ── 출력 ──────────────────────────────────────────────────────────────────────
W   = 106
sep = "=" * W

print(f"\n{sep}")
print(f"  볼린저 밴드(240,2) 종목 필터 비교  (2020-01 ~ 2026-05)")
print(f"  BB 조건: BB상한(240,2) > BB중심(240,2)*1.5  (STD/SMA > 25%, 고변동성 종목)")
print(f"  유니버스: 거래대금 상위 100위  |  수수료 0.3%  |  최대 보유 {MAX_HOLD}거래일")
print(sep)
hdr = (f"  {'조합':<16}  {'거래':>5}  {'승률':>6}  {'평균%':>7}  "
       f"{'Sharpe':>7}  {'누적%':>8}  {'MDD%':>8}  {'평균보유':>8}")
print(hdr)
print("  " + "-" * (W - 2))

base_sharpe = None
for label, td in results.items():
    s = _macro_stats(td)
    if s is None:
        print(f"  {label:<16}  (거래없음)")
        continue
    avg_hold = s["closed"]["hold_days"].mean()
    if base_sharpe is None:
        base_sharpe = s["sharpe"]
        delta_str = ""
    else:
        d = s["sharpe"] - base_sharpe
        delta_str = f"({d:+.2f})"
    print(f"  {label:<16}  {s['total']:>5}건  "
          f"{s['wins']/s['total']*100:>5.1f}%  "
          f"{s['avg']:>+6.2f}%  "
          f"{s['sharpe']:>6.2f}{delta_str:<8}  "
          f"{s['cum']:>+7.1f}%  "
          f"{s['mdd']:>+7.1f}%  "
          f"{avg_hold:>6.1f}일")
print(sep)

# ── 연도별 ────────────────────────────────────────────────────────────────────
print(f"\n  ── 연도별 성과 (건수 / 승률% / 평균수익%) ──")
labels = list(results.keys())
hdr2 = f"  {'연도':>4}"
for lbl in labels:
    hdr2 += f"  {lbl[:12]:>18}"
print(hdr2)
print("  " + "-" * (len(hdr2) - 2))

all_years: set[int] = set()
for td in results.values():
    if td.empty: continue
    cl = td[td["exit_reason"] != "open"]
    if cl.empty: continue
    all_years.update(pd.to_datetime(cl["entry_date"].astype(str)).dt.year.dropna().astype(int))

for yr in sorted(all_years):
    row = f"  {yr:>4}"
    for lbl in labels:
        td = results[lbl]
        if td.empty:
            row += f"  {'N/A':>18}"; continue
        cl = td[td["exit_reason"] != "open"]
        if cl.empty:
            row += f"  {'N/A':>18}"; continue
        mask = pd.to_datetime(cl["entry_date"].astype(str)).dt.year == yr
        pnls = cl.loc[mask, "net_pnl_pct"].dropna()
        if len(pnls) == 0:
            row += f"  {'  -':>18}"
        else:
            wr  = (pnls > 0).mean() * 100
            avg = pnls.mean()
            row += f"  {len(pnls):>3}건/{wr:>5.1f}%/{avg:>+5.1f}%"
    print(row)
print(sep)

# ── 필터 효과 요약 ────────────────────────────────────────────────────────────
base_s = _macro_stats(results["A.기준+MA7"])
print(f"\n  ── 필터 효과 해설 (기준 대비) ──")
for lbl, td in list(results.items())[1:]:
    s = _macro_stats(td)
    if s is None: continue
    trade_red    = (base_s["total"] - s["total"]) / base_s["total"] * 100
    win_delta    = s["wins"]/s["total"]*100 - base_s["wins"]/base_s["total"]*100
    sharpe_delta = s["sharpe"] - base_s["sharpe"]
    mdd_delta    = s["mdd"]    - base_s["mdd"]
    avg_delta    = s["avg"]    - base_s["avg"]
    print(f"  [{lbl}]")
    print(f"    진입 감소  : {trade_red:+.1f}%  ({base_s['total']}건 -> {s['total']}건)")
    print(f"    승률 변화  : {win_delta:+.1f}%p")
    print(f"    평균수익   : {base_s['avg']:+.2f}% -> {s['avg']:+.2f}%  ({avg_delta:+.2f}%p)")
    print(f"    Sharpe     : {base_s['sharpe']:.2f} -> {s['sharpe']:.2f}  ({sharpe_delta:+.2f})")
    print(f"    MDD        : {base_s['mdd']:.1f}% -> {s['mdd']:.1f}%  ({mdd_delta:+.1f}%p)")
print(sep)

# ── BB 조건 통과 종목 정보 ────────────────────────────────────────────────────
print(f"\n  ── BB(240,2) 조건 통과 진입 종목 (D.BR5+BB변동성) ──")
td_d = results.get("D.BR5+BB변동성", pd.DataFrame())
if not td_d.empty:
    cl = td_d[td_d["exit_reason"] != "open"].copy()
    if not cl.empty:
        stock_s = (cl.groupby(["code","name"])
                   .agg(cnt=("net_pnl_pct","count"),
                        avg=("net_pnl_pct","mean"),
                        wr=("net_pnl_pct", lambda x: (x>0).mean()*100))
                   .reset_index()
                   .sort_values("avg", ascending=False))
        for _, r in stock_s.iterrows():
            print(f"    {r['code']} {str(r['name'])[:14]:<14}  "
                  f"{r['cnt']:>2}건  평균{r['avg']:>+6.1f}%  승률{r['wr']:>5.1f}%")
print(sep)
print(f"\n  실행 시간: {time.time()-t0:.1f}초")

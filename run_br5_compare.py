"""BR5 단독 필터 vs BB 매수주시 비교 백테스트."""
import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, 'src')

from ssen.strategy.backtest_pullback import (
    load_ohlcv, load_ssen_leader_filter, filter_universe,
    build_adr_filter, build_bullbear_filter,
    run_macro_comparison, print_macro_comparison, print_report,
)

t0 = time.time()
df = load_ohlcv()
ssen_filter = load_ssen_leader_filter('rank', window=20, rank_top=100)
fdf, codes = filter_universe(df, ratio=5.0)

adr_filter      = build_adr_filter(window=10, thresh=0.45)
bb_watch_filter = build_bullbear_filter(mode='watch')
br5_filter      = build_bullbear_filter(mode='br5_only')

from ssen.strategy.backtest_pullback import _compute_signals, _run_simulation, _macro_stats

print("\n[M] 거시 환경 필터 비교 분석")
print("  이동평균·신호 계산 중...")
sig_df = _compute_signals(fdf, codes)

print("  시뮬레이션 실행:")
results = {}
results["기준(필터없음)"] = _run_simulation(sig_df, codes, 15.0, 120, ssen_filter, None,          "기준")
results["ADR시장폭"]      = _run_simulation(sig_df, codes, 15.0, 120, ssen_filter, adr_filter,     "ADR")
results["BR5단독"]        = _run_simulation(sig_df, codes, 15.0, 120, ssen_filter, br5_filter,     "BR5단독")
results["BB매수주시"]     = _run_simulation(sig_df, codes, 15.0, 120, ssen_filter, bb_watch_filter,"BB주시")

# ── 출력 ──────────────────────────────────────────────────────────────────────
import pandas as pd

W   = 100
sep = "=" * W
print(f"\n{sep}")
print(f"  거시 환경 필터 비교 — BR5 단독 vs BB 매수주시 (2020-01 ~ 2026-05)")
print(f"  BR5 단독: BR5 > 0.55 조건 하나만 적용")
print(f"  BB 매수주시: BR5>0.55 + McClellan>0 + 순상승>0 중 2개 이상")
print(sep)
hdr = f"  {'필터':<15}  {'거래':>5}  {'승률':>6}  {'평균%':>7}  {'Sharpe':>7}  {'누적%':>8}  {'MDD%':>8}  {'Sharpe향상':>10}"
print(hdr)
print("  " + "-" * (W-2))

base_sharpe = None
for label, td in results.items():
    s = _macro_stats(td)
    if s is None:
        print(f"  {label:<15}  (거래없음)")
        continue
    if base_sharpe is None:
        base_sharpe = s["sharpe"]
        delta_str = ""
    else:
        delta_str = f"(+{s['sharpe'] - base_sharpe:.2f})" if s['sharpe'] > base_sharpe else f"({s['sharpe'] - base_sharpe:.2f})"
    print(f"  {label:<15}  {s['total']:>5}건  "
          f"{s['wins']/s['total']*100:>5.1f}%  "
          f"{s['avg']:>+6.2f}%  "
          f"{s['sharpe']:>7.2f}  "
          f"{s['cum']:>+7.1f}%  "
          f"{s['mdd']:>+7.1f}%  "
          f"  {delta_str:>10}")
print(sep)

# ── 연도별 ────────────────────────────────────────────────────────────────────
print(f"\n  ── 연도별 (거래건수 / 승률%) ──")
labels = list(results.keys())
hdr2 = f"  {'연도':>4}"
for lbl in labels:
    hdr2 += f"  {lbl[:8]:>14}"
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
            row += f"  {'N/A':>14}"; continue
        cl = td[td["exit_reason"] != "open"]
        if cl.empty:
            row += f"  {'N/A':>14}"; continue
        mask = pd.to_datetime(cl["entry_date"].astype(str)).dt.year == yr
        pnls = cl.loc[mask, "net_pnl_pct"].dropna()
        if len(pnls) == 0:
            row += f"  {'  -':>14}"
        else:
            wr = (pnls > 0).mean() * 100
            row += f"  {len(pnls):>4}건/{wr:>5.1f}%"
    print(row)
print(sep)

# ── 필터 효과 해설 ────────────────────────────────────────────────────────────
base_s = _macro_stats(results["기준(필터없음)"])
print(f"\n  ── 필터 효과 해설 ──")
for lbl, td in list(results.items())[1:]:
    s = _macro_stats(td)
    if s is None: continue
    trade_red    = (base_s["total"] - s["total"]) / base_s["total"] * 100
    win_delta    = s["wins"]/s["total"]*100 - base_s["wins"]/base_s["total"]*100
    sharpe_delta = s["sharpe"] - base_s["sharpe"]
    mdd_delta    = s["mdd"] - base_s["mdd"]
    print(f"  [{lbl}]")
    print(f"    진입 감소 : {trade_red:+.1f}%  ({base_s['total']}건 -> {s['total']}건)")
    print(f"    승률 변화 : {win_delta:+.1f}%p")
    print(f"    Sharpe    : {base_s['sharpe']:.2f} -> {s['sharpe']:.2f}  ({sharpe_delta:+.2f})")
    print(f"    MDD 변화  : {base_s['mdd']:.1f}% -> {s['mdd']:.1f}%  ({mdd_delta:+.1f}%p)")
print(sep)

print(f"\n  실행 시간: {time.time()-t0:.1f}초")

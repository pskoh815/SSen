"""
고정 손절(-7%) vs MA7 이탈 청산 비교 백테스트.

비교 조합:
  A. 기준          : 필터없음  + MA7 이탈
  B. BR5+MA7       : BR5단독   + MA7 이탈        (이전 최고)
  C. 기준+-7%      : 필터없음  + -7% 고정 손절
  D. BR5+-7%       : BR5단독   + -7% 고정 손절   (신규 테스트)
  E. BB주시+-7%    : BB매수주시 + -7% 고정 손절
"""
import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, 'src')

import pandas as pd
from ssen.strategy.backtest_pullback import (
    load_ohlcv, load_ssen_leader_filter, filter_universe,
    build_bullbear_filter,
    _compute_signals, _run_simulation, _macro_stats,
)

STOP_LOSS = 15.0
MAX_HOLD  = 120
FIXED_STOP = 7.0

t0 = time.time()

# ── 데이터 로드 (1회) ──────────────────────────────────────────────────────────
df           = load_ohlcv()
ssen_filter  = load_ssen_leader_filter('rank', window=20, rank_top=100)
fdf, codes   = filter_universe(df, ratio=5.0)
br5_filter   = build_bullbear_filter(mode='br5_only')
bb_filter    = build_bullbear_filter(mode='watch')

# ── 신호 계산 (1회) ────────────────────────────────────────────────────────────
print("\n  이동평균·신호 계산 중...")
sig_df = _compute_signals(fdf, codes)

# ── 시뮬레이션 ─────────────────────────────────────────────────────────────────
print("  시뮬레이션 실행:")
results = {}
results["A.기준+MA7"]    = _run_simulation(sig_df, codes, STOP_LOSS, MAX_HOLD,
                               ssen_filter, None,       "기준+MA7",   fixed_stop_pct=None)
results["B.BR5+MA7"]     = _run_simulation(sig_df, codes, STOP_LOSS, MAX_HOLD,
                               ssen_filter, br5_filter, "BR5+MA7",    fixed_stop_pct=None)
results["C.기준+-7%"]    = _run_simulation(sig_df, codes, STOP_LOSS, MAX_HOLD,
                               ssen_filter, None,       "기준+-7%",   fixed_stop_pct=FIXED_STOP)
results["D.BR5+-7%"]     = _run_simulation(sig_df, codes, STOP_LOSS, MAX_HOLD,
                               ssen_filter, br5_filter, "BR5+-7%",    fixed_stop_pct=FIXED_STOP)
results["E.BB주시+-7%"]  = _run_simulation(sig_df, codes, STOP_LOSS, MAX_HOLD,
                               ssen_filter, bb_filter,  "BB주시+-7%", fixed_stop_pct=FIXED_STOP)

# ── 출력 ──────────────────────────────────────────────────────────────────────
W   = 108
sep = "=" * W

print(f"\n{sep}")
print(f"  고정 손절(-{FIXED_STOP}%) vs MA7 이탈 청산 비교  (2020-01 ~ 2026-05)")
print(f"  유니버스: 거래대금 상위 100위 이력 보유 종목  |  수수료 0.3%  |  최대 보유 {MAX_HOLD}거래일")
print(f"  MA7 이탈: 종가 < 7일 이동평균 → 청산")
print(f"  고정 손절: 매수가 대비 -{FIXED_STOP}% 도달 → 청산  (MA7 조건 비활성)")
print(sep)
hdr = (f"  {'조합':<14}  {'필터':>8}  {'청산':>6}  {'승률':>6}  "
       f"{'평균%':>7}  {'Sharpe':>7}  {'누적%':>8}  {'MDD%':>8}  {'평균보유':>8}")
print(hdr)
print("  " + "-" * (W - 2))

base_sharpe = None
for label, td in results.items():
    s = _macro_stats(td)
    if s is None:
        print(f"  {label:<14}  (거래없음)")
        continue

    # 청산 사유 분포
    cl = s["closed"]
    exit_counts = cl["exit_reason"].value_counts().to_dict()
    stop_n    = exit_counts.get("stop_loss", 0)
    ma7_n     = exit_counts.get("ma7_break", 0)
    time_n    = exit_counts.get("time_stop", 0)
    avg_hold  = cl["hold_days"].mean()

    exit_str = f"손절{stop_n}/MA7{ma7_n}/시간{time_n}"

    if base_sharpe is None:
        base_sharpe = s["sharpe"]
        delta_str = ""
    else:
        d = s['sharpe'] - base_sharpe
        delta_str = f"({d:+.2f})"

    print(f"  {label:<14}  {exit_str:>12}  {s['total']:>4}건  "
          f"{s['wins']/s['total']*100:>5.1f}%  "
          f"{s['avg']:>+6.2f}%  "
          f"{s['sharpe']:>6.2f}{delta_str:<7}  "
          f"{s['cum']:>+7.1f}%  "
          f"{s['mdd']:>+7.1f}%  "
          f"{avg_hold:>6.1f}일")

print(sep)

# ── 연도별 ────────────────────────────────────────────────────────────────────
print(f"\n  ── 연도별 성과 (건수 / 승률% / 평균수익%) ──")
labels = list(results.keys())
hdr2 = f"  {'연도':>4}"
for lbl in labels:
    hdr2 += f"  {lbl[:10]:>17}"
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
            row += f"  {'N/A':>17}"; continue
        cl = td[td["exit_reason"] != "open"]
        if cl.empty:
            row += f"  {'N/A':>17}"; continue
        mask = pd.to_datetime(cl["entry_date"].astype(str)).dt.year == yr
        pnls = cl.loc[mask, "net_pnl_pct"].dropna()
        if len(pnls) == 0:
            row += f"  {'  -':>17}"
        else:
            wr  = (pnls > 0).mean() * 100
            avg = pnls.mean()
            row += f"  {len(pnls):>3}건/{wr:>5.1f}%/{avg:>+5.1f}%"
    print(row)
print(sep)

# ── 청산 사유별 평균 수익 ────────────────────────────────────────────────────
print(f"\n  ── 청산 사유별 평균 수익률 ──")
for label, td in results.items():
    s = _macro_stats(td)
    if s is None: continue
    cl = s["closed"]
    print(f"  [{label}]")
    for reason, grp in cl.groupby("exit_reason"):
        pnls = grp["net_pnl_pct"].dropna()
        if len(pnls) == 0: continue
        wr = (pnls > 0).mean() * 100
        reason_map = {"ma7_break": "MA7이탈", "stop_loss": f"-{FIXED_STOP}%손절", "time_stop": "시간청산"}
        print(f"    {reason_map.get(reason, reason):<10}: {len(pnls):>4}건  "
              f"승률{wr:5.1f}%  평균{pnls.mean():>+6.2f}%  최소{pnls.min():>+6.2f}%  최대{pnls.max():>+6.2f}%")
print(sep)

print(f"\n  실행 시간: {time.time()-t0:.1f}초")

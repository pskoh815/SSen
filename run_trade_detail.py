"""B조합(BR5+MA7, 12건) + D조합(BR5+BB변동성, 4건) 상세 거래 내역."""
import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, 'src')

import pandas as pd
from ssen.strategy.backtest_pullback import (
    load_ohlcv, load_ssen_leader_filter, filter_universe,
    build_bullbear_filter,
    _compute_signals, _run_simulation,
)

t0 = time.time()
df          = load_ohlcv()
ssen_filter = load_ssen_leader_filter('rank', window=20, rank_top=100)
fdf, codes  = filter_universe(df, ratio=5.0)
br5_filter  = build_bullbear_filter(mode='br5_only')

print("  신호 계산 중...")
sig_df = _compute_signals(fdf, codes)

td_b = _run_simulation(sig_df, codes, 15.0, 120, ssen_filter, br5_filter, "B.BR5+MA7",    bb_volatility=False)
td_d = _run_simulation(sig_df, codes, 15.0, 120, ssen_filter, br5_filter, "D.BR5+BB변동성", bb_volatility=True)

def print_detail(label, td):
    cl = td[td["exit_reason"] != "open"].copy()
    if cl.empty:
        print("  거래 없음")
        return

    cl = cl.sort_values("entry_date").reset_index(drop=True)
    pnls   = cl["net_pnl_pct"].dropna()
    wins   = (pnls > 0).sum()
    cumret = (1 + pnls / 100).cumprod()
    mdd    = ((cumret - cumret.cummax()) / cumret.cummax() * 100).min()

    W   = 108
    sep = "=" * W
    print(f"\n{sep}")
    print(f"  {label}  —  전체 {len(cl)}건 청산")
    print(f"  승률 {wins/len(pnls)*100:.1f}%  |  평균 {pnls.mean():+.2f}%  |  "
          f"누적 {(cumret.iloc[-1]-1)*100:+.1f}%  |  MDD {mdd:.1f}%")
    print(sep)

    hdr = (f"  {'#':>3}  {'종목명':<14}  {'코드':>6}  "
           f"{'진입일':>10}  {'청산일':>10}  "
           f"{'진입가':>8}  {'청산가':>8}  {'최고가':>8}  "
           f"{'보유일':>4}  {'최고%':>7}  {'수익%':>7}  {'반납%':>7}  {'사유'}")
    print(hdr)
    print("  " + "-" * (W - 2))

    for i, r in cl.iterrows():
        peak_str = f"{r['peak_pct']:>+6.1f}%" if pd.notna(r.get('peak_pct')) else "    N/A"
        give_str = f"{-r['giveback_pct']:>+6.1f}%" if pd.notna(r.get('giveback_pct')) else "    N/A"
        reason_map = {"ma7_break": "MA7이탈", "stop_loss": "손절", "time_stop": "시간"}
        reason = reason_map.get(r["exit_reason"], r["exit_reason"])
        net = r["net_pnl_pct"]
        mark = "▲" if net > 0 else "▼"
        print(f"  {i+1:>3}  {str(r['name'])[:14]:<14}  {r['code']:>6}  "
              f"{str(r['entry_date']):>10}  {str(r['exit_date']):>10}  "
              f"{int(r['entry_price']):>8,}  {int(r['exit_price']):>8,}  "
              f"{int(r['peak_price']) if pd.notna(r.get('peak_price')) else 0:>8,}  "
              f"{int(r['hold_days']):>4}일  "
              f"{peak_str}  {net:>+6.1f}%{mark}  {give_str}  {reason}")

    print(sep)
    print(f"\n  수익률 분포:")
    bins   = [-999, -10, -5, 0, 5, 10, 20, 999]
    labels = ["<-10%", "-10~-5%", "-5~0%", "0~+5%", "+5~+10%", "+10~+20%", ">+20%"]
    cut = pd.cut(pnls, bins=bins, labels=labels)
    for lbl, cnt in cut.value_counts().sort_index().items():
        if cnt == 0: continue
        bar = "█" * cnt
        print(f"    {lbl:>10} : {cnt:>2}건  {bar}")
    print()

print_detail("B조합  BR5단독 + MA7이탈",        td_b)
print_detail("D조합  BR5단독 + BB변동성(240,2) + MA7이탈", td_d)
print(f"  실행 시간: {time.time()-t0:.1f}초")

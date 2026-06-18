import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 백테스트 결과를 직접 재현
results_summary = {
    "기준(필터없음)":  dict(trades=59,  winrate=35.6, avg_pnl=+0.49, sharpe=1.22, cum=+20.4, mdd=-25.8, sharpe_delta=0.0),
    "ADR시장폭":       dict(trades=44,  winrate=31.8, avg_pnl=+0.66, sharpe=1.47, cum=+21.6, mdd=-26.4, sharpe_delta=+0.26),
    "BB매수주시":      dict(trades=27,  winrate=25.9, avg_pnl=+1.47, sharpe=2.62, cum=+35.8, mdd=-17.5, sharpe_delta=+1.40),
    "BB매수확정":      dict(trades=13,  winrate=23.1, avg_pnl=+1.77, sharpe=2.53, cum=+18.2, mdd= -8.2, sharpe_delta=+1.31),
    "ADR+BB결합":      dict(trades=25,  winrate=24.0, avg_pnl=+1.21, sharpe=2.13, cum=+24.2, mdd=-17.5, sharpe_delta=+0.91),
}

yearly = {
    2021: dict(baseline=(18,44.4), adr=(13,38.5), bb_watch=(13,30.8), bb_confirm=(6,16.7), adr_bb=(11,27.3)),
    2022: dict(baseline=( 2, 0.0), adr=( 2, 0.0), bb_watch=( 1, 0.0), bb_confirm=(1, 0.0), adr_bb=( 1, 0.0)),
    2023: dict(baseline=( 7,28.6), adr=( 4, 0.0), bb_watch=( 1, 0.0), bb_confirm=(1, 0.0), adr_bb=( 1, 0.0)),
    2024: dict(baseline=(10,30.0), adr=( 6,50.0), bb_watch=( 2, 0.0), bb_confirm=(1, 0.0), adr_bb=( 2, 0.0)),
    2025: dict(baseline=(10,20.0), adr=(10,20.0), bb_watch=( 3, 0.0), bb_confirm=(2, 0.0), adr_bb=( 3, 0.0)),
    2026: dict(baseline=(12,50.0), adr=( 9,44.4), bb_watch=( 7,42.9), bb_confirm=(2,100.), adr_bb=( 7,42.9)),
}

W = 100
sep = "=" * W
print(f"\n{sep}")
print(f"  눌림목 백테스트 v3 — 거시 환경 필터 비교 (2020-01 ~ 2026-05)")
print(f"  유니버스: 거래대금 상위 100위 이력 보유 종목 (SSen rank 필터)")
print(f"  조건: 완전 정배열 + MA5 52주 신고가 + 눌림목 (7봉 최소 몸통·MA7 근접)")
print(f"  수수료: 0.3%  |  손절: MA7 이탈 / 안전 -15%  |  최대 보유: 120거래일")
print(sep)
print(f"  {'필터':<15}  {'거래':>5}  {'승률':>6}  {'평균%':>7}  {'Sharpe':>7}  {'누적%':>8}  {'MDD%':>8}  {'Sharpe향상':>10}")
print("  " + "-" * (W-2))
for label, r in results_summary.items():
    delta_str = f"(+{r['sharpe_delta']:.2f})" if r['sharpe_delta'] > 0 else ""
    print(f"  {label:<15}  {r['trades']:>5}건  "
          f"{r['winrate']:>5.1f}%  "
          f"{r['avg_pnl']:>+6.2f}%  "
          f"{r['sharpe']:>7.2f}  "
          f"{r['cum']:>+7.1f}%  "
          f"{r['mdd']:>+7.1f}%  "
          f"  {delta_str:>10}")
print(sep)

print(f"\n  ── 연도별 (거래건수 / 승률%) ──")
hdr = f"  {'연도':>4}  {'기준':>12}  {'ADR':>12}  {'BB매수주시':>12}  {'BB매수확정':>12}  {'ADR+BB':>12}"
print(hdr)
print("  " + "-" * (len(hdr)-2))
keys = ['baseline','adr','bb_watch','bb_confirm','adr_bb']
for yr, row in yearly.items():
    line = f"  {yr:>4}"
    for k in keys:
        n, wr = row[k]
        line += f"  {n:>3}건/{wr:>5.1f}%"
    print(line)
print(sep)

print(f"""
  ── 필터 효과 요약 ──

  [ADR 시장폭 (window=10, thresh=0.45)]
    진입 감소 : -25.4%  (59건 → 44건)
    Sharpe    : 1.22 → 1.47  (+0.26)  ▲ 소폭 개선
    MDD       : -25.8% → -26.4%  (-0.6%p)  ▼ 오히려 소폭 악화
    누적 수익 : +20.4% → +21.6%

  [BB 매수주시 (BR5>0.55 AND McClellan>0 AND 순상승>0 중 2개 이상)]
    진입 감소 : -54.2%  (59건 → 27건)
    Sharpe    : 1.22 → 2.62  (+1.40)  ▲▲▲ 대폭 개선
    MDD       : -25.8% → -17.5%  (+8.3%p)  ▲ 리스크 감소
    누적 수익 : +20.4% → +35.8%  ▲ 수익 증가

  [BB 매수확정 (3일 연속 2개 이상)]
    진입 감소 : -78.0%  (59건 → 13건)  ※ 거래 수 너무 적음 → 통계 신뢰도 낮음
    Sharpe    : 1.22 → 2.53  (+1.31)  ▲▲▲ 대폭 개선
    MDD       : -25.8% →  -8.2%  (+17.6%p)  ▲▲ 리스크 최대 감소
    누적 수익 : +20.4% → +18.2%  (거래 수 부족으로 절대 수익은 낮음)

  [ADR + BB매수주시 결합]
    진입 감소 : -57.6%  (59건 → 25건)
    Sharpe    : 1.22 → 2.13  (+0.91)
    MDD       : -25.8% → -17.5%  (+8.3%p)
    (ADR과 BB를 AND 결합해도 BB 단독보다 Sharpe 낮음)
""")
print(sep)
print(f"  결론: BB 매수주시 필터가 ADR 필터 대비 Sharpe +1.14 추가 개선 (1.47→2.62)")
print(f"        거래 수: 27건으로 통계적 유의성은 제한적 — 더 긴 기간 검증 필요")
print(sep)

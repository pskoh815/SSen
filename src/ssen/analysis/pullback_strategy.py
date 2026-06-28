# -*- coding: utf-8 -*-
"""§P 종목 자체 추세 정배열 전략 — 트레이드 탭("기준봉 눌림목 타점 전략" 대체, 2026-06-26).

기존 "기준봉 눌림목 타점 전략"은 10년 백테스트(2016~2025) 결과 10년 복리 -67.4%로
구조적 손실이 확인되어 폐기. 대체 전략(EasyLanguage 스펙 기반, A/B 테스트로 검증
— 10년 복리 +126.89%, 최악 연간 MDD -29.6%, 코스피EMA20/60·BR5·20일선이탈비율
매크로 필터/COMPOSITE 변동성 응축 조건/볼린저 상한선 상승 조건/MSum money-flow
조건 등을 추가로 테스트했으나 전부 이 원본보다 못했음 — 종목 자체의 추세 조건이
이미 충분히 선별적이라 추가 필터가 좋은 기회까지 같이 잘라내는 경향이 일관되게
나타남):

  Condition2 (중장기 정배열): EMA(C,60) > EMA(C,120) > EMA(C,240)
  Condition3 (중간값 정배열): SD1 > SD2 > SD3
    SD1=(Highest(H,9)+Lowest(L,9))/2, SD2=(26일), SD3=(52일)
  Condition6 (지지선 유지): Lowest(L,15) > (Highest(H,60)+Lowest(L,60))/2
                            AND Lowest(L,20) > EMA(C,60)
  Condition7 = Condition2 AND Condition3 AND Condition6 → 신호일 종가에 매수

  청산: 기준선 지지 + RS 추세 전략(api_supertrend_trades)과 동일
    -5% 손절(1차 익절 전, 전량) / +10% 1차 익절(1/3 매도) 후 잔여 2/3는
    USAF>=USAF_THRESHOLD인 날 전량 매도. 미발생 시 exit_reason="open".

데이터 소스: data/market/ohlcv (전 종목)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .period_analysis import _clean
from .supertrend_strategy import (
    STOP_LOSS_PCT, TP1_PCT, TP1_RATIO,
    _add_usaf_signal, _empty_summary, _load_all_ohlcv, _portfolio_summarize,
)

EMA_LONG_PERIODS    = (60, 120, 240)
SD_PERIODS          = (9, 26, 52)
SUPPORT_MID_WINDOW  = 60
SUPPORT_LOW_WINDOWS = (15, 20)
MIN_BARS            = max(EMA_LONG_PERIODS) + 60


def api_pullback_trades(start: date, end: date) -> dict[str, Any]:
    """§P 종목 자체 추세 정배열 전략 백테스트 (전 종목 대상)."""
    load_buffer_days = int(max(EMA_LONG_PERIODS) * 1.6) + 90
    load_start = start - timedelta(days=load_buffer_days)
    load_end = end + timedelta(days=60)

    ohlcv = _load_all_ohlcv(load_start, load_end)
    if ohlcv.empty:
        return dict(trades=[], summary=_empty_summary())

    trades: list[dict] = []
    for code, g in ohlcv.groupby("code"):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < MIN_BARS:
            continue

        close, high, low = g["close"], g["high"], g["low"]
        ema60 = close.ewm(span=EMA_LONG_PERIODS[0], adjust=False).mean()
        ema120 = close.ewm(span=EMA_LONG_PERIODS[1], adjust=False).mean()
        ema240 = close.ewm(span=EMA_LONG_PERIODS[2], adjust=False).mean()

        sd1 = (high.rolling(SD_PERIODS[0]).max() + low.rolling(SD_PERIODS[0]).min()) / 2
        sd2 = (high.rolling(SD_PERIODS[1]).max() + low.rolling(SD_PERIODS[1]).min()) / 2
        sd3 = (high.rolling(SD_PERIODS[2]).max() + low.rolling(SD_PERIODS[2]).min()) / 2
        mid60 = (high.rolling(SUPPORT_MID_WINDOW).max() + low.rolling(SUPPORT_MID_WINDOW).min()) / 2

        cond2 = (ema60 > ema120) & (ema120 > ema240)
        cond3 = (sd1 > sd2) & (sd2 > sd3)
        cond6 = (low.rolling(SUPPORT_LOW_WINDOWS[0]).min() > mid60) & \
                (low.rolling(SUPPORT_LOW_WINDOWS[1]).min() > ema60)
        signal_t = (cond2 & cond3 & cond6).fillna(False).to_numpy()

        g = _add_usaf_signal(g)
        signal_strong = g["signal_strong"].to_numpy()
        close_np = close.to_numpy()
        trading_dates = g["date"].tolist()
        n = len(g)

        cursor = 0
        for sig in range(n):
            if sig < cursor or not bool(signal_t[sig]):
                continue

            entry_idx = sig
            entry_price = float(close_np[entry_idx])

            # ── 청산 (기준선 지지 + RS 추세 전략과 동일)
            exit_idx = exit_date = exit_price = exit_reason = None
            pnl_pct = hold_days = None
            tp1_pct = None
            for m in range(entry_idx + 1, n):
                pct = float((close_np[m] - entry_price) / entry_price * 100)
                if tp1_pct is None:
                    if pct <= STOP_LOSS_PCT:
                        exit_idx, pnl_pct, exit_reason = m, pct, "stop_loss"
                        break
                    if pct >= TP1_PCT:
                        tp1_pct = pct
                else:
                    if signal_strong[m]:
                        blended = TP1_RATIO * tp1_pct + (1 - TP1_RATIO) * pct
                        exit_idx, pnl_pct, exit_reason = m, blended, "tp1_then_trend_exit"
                        break

            if exit_idx is not None:
                exit_price = float(close_np[exit_idx])
                exit_date = str(trading_dates[exit_idx])
                pnl_pct = round(pnl_pct, 2)
                hold_days = exit_idx - entry_idx
                cursor = exit_idx + 1
            else:
                exit_reason = "open"
                cursor = n

            entry_date = trading_dates[entry_idx]
            if not (start <= entry_date <= end):
                continue

            trades.append(dict(
                code=code,
                name=str(g["name"].iloc[entry_idx]),
                breakout_date=str(trading_dates[sig]),
                signal_date=str(trading_dates[sig]),
                entry_date=str(entry_date),
                entry_price=round(entry_price),
                exit_date=exit_date,
                exit_price=round(exit_price) if exit_price is not None else None,
                exit_reason=exit_reason,
                pnl_pct=pnl_pct,
                hold_days=hold_days,
            ))

    trades.sort(key=lambda t: (t["entry_date"], t["code"]))
    taken, summary = _portfolio_summarize(trades)
    return dict(trades=_clean(taken), summary=summary)

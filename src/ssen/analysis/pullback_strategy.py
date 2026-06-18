# -*- coding: utf-8 -*-
"""§P 기준봉 눌림목 타점 전략 — 트레이드 탭.

전략 로직(EasyLanguage 원문 기반):
  1단계 — t일 기준봉(SignalT) 판별:
    HM = MAX(SuperTrend(21,3), IchimokuKijun(26), VW(120, 거래량가중평균가),
             EMA(C,60), EMA(C,120), EMA(C,240))
    VPower = ((종가-시가)/ATR(21)) * (거래량/MA(거래량,21))  (분모 0이면 0)
    SignalT = (전일종가 < HM) AND (금일종가 > HM)            # HM 상향 돌파
              AND 최근 120봉 중 거래대금>1,000억(1e11)인 봉이 1개 이상
              AND VPower > 1.5

  2단계 — t+1~t+4 눌림목 동적 추적:
    BaseLow=L[t], BaseVol=V[t], MaxHighSinceBase는 매일 고가로 갱신
    HalfPrice = (BaseLow + MaxHighSinceBase) / 2
    추적 중 하루라도 L < HalfPrice → 해당 기준봉 사이클 영구 탈락
    탈락 전, 당일 거래량 < BaseVol AND |종가-시가| <= 시가*3% 인 날의
    종가에 매수(IsValidPattern). 4영업일 내 미충족 시 사이클 소멸.

  3단계 — 청산: 기준선 지지 + RS 추세 전략(api_supertrend_trades)과 동일
    -5% 손절(1차 익절 전, 전량) / +10% 1차 익절(1/3 매도) 후 잔여 2/3는
    USAF>=USAF_THRESHOLD인 날 전량 매도. 미발생 시 exit_reason="open".

데이터 소스: data/market/ohlcv (전 종목)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .period_analysis import _clean
from .supertrend_strategy import (
    MIN_BARS, STOP_LOSS_PCT, TP1_PCT, TP1_RATIO,
    _add_usaf_signal, _empty_summary, _load_all_ohlcv, _portfolio_summarize, _supertrend,
)

KIJUN_PERIOD     = 26    # IchimokuKijun = (Highest(H,26)+Lowest(L,26))/2
VW_PERIOD        = 120   # Period5: 거래량가중평균가(VW) 산출 윈도우
EMA_PERIODS      = (60, 120, 240)  # EM4/EM5/EM6
ATR_PERIOD       = 21    # Period: ATR / VPower의 MA(V,21) 기간
VPOWER_MIN       = 1.5   # VFilter = VPower > 1.5
AMOUNT_THRESHOLD = 100_000_000_000  # 거래대금 1,000억원
AMOUNT_LOOKBACK  = 120   # CountIF(거래대금>1,000억, 120) >= 1
AMOUNT_COUNT_MIN = 1
PULLBACK_WINDOW  = 4     # t+1 ~ t+4
DOJI_BODY_PCT    = 0.03  # |종가-시가| <= 시가 * 3%


def api_pullback_trades(start: date, end: date) -> dict[str, Any]:
    """§P 기준봉 눌림목 타점 전략 백테스트 (전 종목 대상)."""
    load_buffer_days = int(max(EMA_PERIODS) * 1.6) + int(AMOUNT_LOOKBACK * 1.6)
    load_start = start - timedelta(days=load_buffer_days)
    load_end = end + timedelta(days=60)

    ohlcv = _load_all_ohlcv(load_start, load_end)
    if ohlcv.empty:
        return dict(trades=[], summary=_empty_summary())

    close = ohlcv.pivot(index="date", columns="code", values="close").sort_index()
    high = ohlcv.pivot(index="date", columns="code", values="high").sort_index()
    low = ohlcv.pivot(index="date", columns="code", values="low").sort_index()
    open_ = ohlcv.pivot(index="date", columns="code", values="open").sort_index()
    volume = ohlcv.pivot(index="date", columns="code", values="volume").sort_index()
    amount = ohlcv.pivot(index="date", columns="code", values="amount").sort_index()

    # ── HM 구성요소(SuperTrend 제외) + VPower 근사치를 벡터화로 한 번에 계산해
    #    후보 종목을 먼저 좁힌다 (전 종목 순차 슈퍼트렌드 계산은 비용이 크므로).
    kijun = (high.rolling(KIJUN_PERIOD).max() + low.rolling(KIJUN_PERIOD).min()) / 2
    vol_sum = volume.rolling(VW_PERIOD).sum()
    vw = ((close * volume).rolling(VW_PERIOD).sum() / vol_sum).where(vol_sum > 0, close)
    ema = {p: close.ewm(span=p, adjust=False).mean() for p in EMA_PERIODS}
    hm_no_st = pd.DataFrame(
        np.maximum.reduce([
            kijun.to_numpy(), vw.to_numpy(),
            ema[EMA_PERIODS[0]].to_numpy(), ema[EMA_PERIODS[1]].to_numpy(), ema[EMA_PERIODS[2]].to_numpy(),
        ]),
        index=close.index, columns=close.columns,
    )

    prev_close = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - prev_close).abs(), (low - prev_close).abs()))
    atr_approx = tr.ewm(alpha=1 / ATR_PERIOD, adjust=False).mean()
    vol_ma = volume.rolling(ATR_PERIOD).mean()
    vpower_approx = ((close - open_) / atr_approx) * (volume / vol_ma)
    vfilter_approx = vpower_approx > VPOWER_MIN

    amount_count = (amount > AMOUNT_THRESHOLD).rolling(AMOUNT_LOOKBACK).sum() >= AMOUNT_COUNT_MIN
    breakout_approx = (prev_close < hm_no_st) & (close > hm_no_st)

    prefilter = breakout_approx & amount_count & vfilter_approx
    candidate_codes = [c for c in prefilter.columns if prefilter[c].any()]
    if not candidate_codes:
        return dict(trades=[], summary=_empty_summary())

    trades: list[dict] = []
    for code, g in ohlcv[ohlcv["code"].isin(candidate_codes)].groupby("code"):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < MIN_BARS:
            continue
        g = _supertrend(g)
        g = _add_usaf_signal(g)

        trading_dates = g["date"].tolist()
        n = len(g)
        close_np = g["close"].to_numpy()
        open_np = g["open"].to_numpy()
        high_np = g["high"].to_numpy()
        low_np = g["low"].to_numpy()
        volume_np = g["volume"].to_numpy()
        atr_np = g["atr"].to_numpy()
        supertrend_np = g["supertrend"].to_numpy()
        signal_strong = g["signal_strong"].to_numpy()

        kijun_c = kijun[code].reindex(trading_dates).to_numpy()
        vw_c = vw[code].reindex(trading_dates).to_numpy()
        ema_c = [ema[p][code].reindex(trading_dates).to_numpy() for p in EMA_PERIODS]
        amount_count_c = amount_count[code].reindex(trading_dates).to_numpy()

        hm = np.maximum.reduce([supertrend_np, kijun_c, vw_c, *ema_c])

        vol_ma_c = pd.Series(volume_np).rolling(ATR_PERIOD).mean().to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            vpower = np.where((vol_ma_c > 0) & (atr_np > 0),
                               ((close_np - open_np) / atr_np) * (volume_np / vol_ma_c), 0.0)
        vfilter = vpower > VPOWER_MIN

        prev_close_np = np.r_[np.nan, close_np[:-1]]
        signal_t = (prev_close_np < hm) & (close_np > hm) & amount_count_c.astype(bool) & vfilter

        cursor = 0
        for sig in range(n):
            if sig < cursor or not bool(signal_t[sig]):
                continue

            # ── 2단계: t+1~t+4 눌림목 동적 추적 (HalfPrice 영구탈락 / 거래량감소+단봉)
            base_low = low_np[sig]
            base_vol = volume_np[sig]
            max_high = high_np[sig]
            entry_idx = None
            for off in range(1, PULLBACK_WINDOW + 1):
                i = sig + off
                if i >= n:
                    break
                if high_np[i] > max_high:
                    max_high = high_np[i]
                half_price = (base_low + max_high) / 2
                if low_np[i] < half_price:
                    break  # 영구 탈락
                cond_vol = volume_np[i] < base_vol
                cond_candle = abs(close_np[i] - open_np[i]) <= open_np[i] * DOJI_BODY_PCT
                if cond_vol and cond_candle:
                    entry_idx = i
                    break

            if entry_idx is None:
                cursor = sig + 1
                continue

            entry_price = float(close_np[entry_idx])

            # ── 3단계: 청산 (기준선 지지 + RS 추세 전략과 동일)
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

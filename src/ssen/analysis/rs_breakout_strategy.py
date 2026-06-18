# -*- coding: utf-8 -*-
"""§R RS≥85 + 거래대금 1천억 + 슈퍼트렌드 돌파 전략 — 트레이드 탭.

전략 로직:
  1. RS점수(상대강도, 0~100) 산출: 20/60/120/250거래일 수익률을 종목별로 계산 후
     해당일 기준 전체 종목 대비 백분위(rank pct)로 환산, 가중합
       가중치 — 20일 10% / 60일 30% / 120일 20% / 250일 40%
       (docs/kkangto_spec.md RS 정의: RS_W_1M=0.10, RS_W_3M=0.30, RS_W_6M=0.20, RS_W_12M=0.40)
  2. RS≥85가 된 날(Q) 이후 50거래일 이내(당일 포함)에 당일 거래대금 ≥1,000억원인 날이
     존재하는 종목만 후보로 선정
  3. Q일 이후 슈퍼트렌드(21,3) 첫 상승전환일(B)에서, 슈퍼트렌드 값이
     52주 신고가 대비 -15% 이내 (supertrend >= high52w * 0.85)
  4. B일 이후 3거래일 이내 단봉(|종가-시가|/시가 < 3%)이 발생한 날(N)의 종가에 매수
  5. 청산: 매수가 대비 종가 기준으로
       - -8% 이하 (1차 익절 전): 손절, 보유 전량 매도 (exit_reason="stop_loss")
       - +16% 이상 (1차 익절): 보유 물량의 1/3만 매도 (TP1_RATIO), 잔여 2/3는
         USAF v2.0(만능 매도 가속 감지 공식) 신호로 추세 추종
       - 1차 익절 이후, USAF >= USAF_THRESHOLD(0.5)인 날 잔여 2/3 전량 매도
         (exit_reason="tp1_then_trend_exit")
       - 위 조건이 발생하지 않은 날 종가가 SMA20 아래로 마감하면 그 시점 보유분
         전량 매도 (exit_reason="ma20_break")
       기간 내 위 조건이 모두 발생하지 않으면 exit_reason="open"
     pnl_pct는 1차 익절이 있을 경우 1차(1/3)/잔여 청산(2/3) 수익률의 가중평균
     (TP1_RATIO : 1-TP1_RATIO)

데이터 소스: data/market/ohlcv (data.go.kr 수집 일별 OHLCV, 전 종목)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from .period_analysis import _clean
from .perf_timer import timed_db_query
from .supertrend_strategy import (
    _FOHLCV, _supertrend, _add_52w_high, _add_usaf_signal, _empty_summary, _summarize,
    HIGH52W_WINDOW, NEAR_HIGH_PCT, BREAKOUT_WINDOW, NARROW_BODY_PCT,
    ST_PERIOD,
)

RS_WINDOWS      = {20: 0.10, 60: 0.30, 120: 0.20, 250: 0.40}  # 기간별 가중치
RS_MIN          = 85.0          # RS점수 하한
AMOUNT_THRESHOLD = 100_000_000_000  # 거래대금 1,000억원
AMOUNT_LOOKAHEAD = 50            # RS≥85일 이후 거래대금 조건 탐색 기간(거래일)
MA_EXIT         = 20            # 매도 기준 이동평균선 기간

# ── 청산(매도) 파라미터: 손절/1차 익절/추세추종/이평 이탈 ──────────────────────
STOP_LOSS_PCT = -8.0   # 매수가 대비 -8% 손절 (1차 익절 전)
TP1_PCT       = 16.0   # 매수가 대비 +16% 이상 시 1차 익절
TP1_RATIO     = 1.0 / 3.0  # 1차 익절 매도 비율(1/3), 잔여 2/3는 USAF 추세추종


def _load_market_ohlcv(start: date, end: date) -> pd.DataFrame:
    sql = f"""
        SELECT date, code, name, open, high, low, close, volume, amount
        FROM   read_parquet('{_FOHLCV}', hive_partitioning=true)
        WHERE  date BETWEEN '{start}' AND '{end}'
        ORDER BY code, date
    """
    con = duckdb.connect()
    try:
        with timed_db_query():
            df = con.execute(sql).fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def _load_close_matrix(start: date, end: date) -> pd.DataFrame:
    """RS 계산용 종가 행렬 — date/code/close만 로딩 (ORDER BY 생략, pivot 후 정렬)."""
    sql = f"""
        SELECT date, code, close
        FROM   read_parquet('{_FOHLCV}', hive_partitioning=true)
        WHERE  date BETWEEN '{start}' AND '{end}'
    """
    con = duckdb.connect()
    try:
        with timed_db_query():
            df = con.execute(sql).fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.pivot(index="date", columns="code", values="close").sort_index()


def _compute_rs(close: pd.DataFrame) -> pd.DataFrame:
    """일자×종목 종가 행렬에서 RS점수(가중 백분위) 행렬 산출."""
    rs = None
    for window, weight in RS_WINDOWS.items():
        pct = close.pct_change(window)
        rank_pct = pct.rank(axis=1, pct=True) * 100
        term = rank_pct * weight
        rs = term if rs is None else rs + term
    return rs


def api_rs_matrix(start: date, end: date) -> pd.DataFrame:
    """기간 [start,end]의 일자×종목 RS점수 행렬 (index=date, columns=code)."""
    rs_lookback_days = int(max(RS_WINDOWS) * 1.6)  # 250거래일 ≈ 400 캘린더일
    load_start = start - timedelta(days=rs_lookback_days)
    close = _load_close_matrix(load_start, end)
    if close.empty:
        return pd.DataFrame()
    rs = _compute_rs(close)
    return rs.loc[(rs.index >= start) & (rs.index <= end)]


def api_rs_breakout_trades(start: date, end: date) -> dict[str, Any]:
    """§R RS≥85 + 거래대금 1천억 + 슈퍼트렌드 돌파 전략 백테스트."""
    rs_lookback_days = int(max(RS_WINDOWS) * 1.6)  # 250거래일 ≈ 400 캘린더일
    load_start = start - timedelta(days=rs_lookback_days + int(HIGH52W_WINDOW * 1.6))
    load_end = end + timedelta(days=90)

    df = _load_market_ohlcv(load_start, load_end)
    if df.empty:
        return dict(trades=[], summary=_empty_summary())

    close = df.pivot(index="date", columns="code", values="close").sort_index()
    amount = df.pivot(index="date", columns="code", values="amount").sort_index()
    rs = _compute_rs(close)

    # RS≥85일 이후 AMOUNT_LOOKAHEAD거래일 이내(당일 포함) 거래대금 최댓값
    amt_fwd_max = amount[::-1].rolling(AMOUNT_LOOKAHEAD, min_periods=1).max()[::-1]
    qualify = (rs >= RS_MIN) & (amt_fwd_max >= AMOUNT_THRESHOLD)
    qualify = qualify.loc[(qualify.index >= start) & (qualify.index <= end)]

    trades: list[dict] = []
    for code, g in df.groupby("code"):
        if code not in qualify.columns:
            continue
        qcol = qualify[code]
        if not qcol.any():
            continue
        q_date = qcol[qcol].index[0]

        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < ST_PERIOD + BREAKOUT_WINDOW + MA_EXIT + 2:
            continue
        g = _supertrend(g)
        g = _add_52w_high(g)
        g = _add_usaf_signal(g)
        g["sma20"] = g["close"].rolling(MA_EXIT).mean()

        trading_dates = g["date"].tolist()
        try:
            q_idx = trading_dates.index(q_date)
        except ValueError:
            continue

        n = len(g)
        trend = g["trend"].values
        supertrend = g["supertrend"].values
        high52w = g["high52w"].values
        close_arr = g["close"].values
        open_arr = g["open"].values
        sma20 = g["sma20"].values
        signal_strong = g["signal_strong"].values
        rs_code = rs[code].reindex(trading_dates).values if code in rs.columns else np.full(n, np.nan)

        cursor = q_idx
        while cursor < n:
            # 1) Q일 이후 첫 슈퍼트렌드 상승전환(B) — 52주 신고가 -15% 이내
            b_idx = None
            for i in range(max(cursor, 1), n):
                if trend[i - 1] == -1 and trend[i] == 1:
                    if np.isnan(high52w[i]) or high52w[i] <= 0:
                        continue
                    if supertrend[i] >= high52w[i] * (1 - NEAR_HIGH_PCT / 100):
                        b_idx = i
                        break
            if b_idx is None:
                break

            # 2) B일 이후 BREAKOUT_WINDOW일 이내 단봉(N)
            n_idx = None
            for off in range(1, BREAKOUT_WINDOW + 1):
                ni = b_idx + off
                if ni >= n:
                    break
                o = open_arr[ni]
                body_pct = abs(close_arr[ni] - o) / o * 100 if o else None
                if body_pct is None or body_pct >= NARROW_BODY_PCT:
                    continue
                n_idx = ni
                break
            if n_idx is None:
                cursor = b_idx + 1
                continue

            entry_idx = n_idx
            entry_price = float(close_arr[entry_idx])

            # 3) 청산: -8% 손절(1차 익절 전, 전량) / +16% 1차 익절(1/3 매도) 후
            #    잔여 2/3는 USAF>=USAF_THRESHOLD인 날 전량 매도. 위 조건이 발생하지
            #    않은 날 종가가 SMA20 아래로 마감하면 그 시점 보유분 전량 매도
            exit_idx = exit_reason = None
            pnl_pct = None
            tp1_pct = None  # 1차(1/3) 매도 시점의 수익률
            for m in range(entry_idx + 1, n):
                pct = float((close_arr[m] - entry_price) / entry_price * 100)
                ma20_break = not np.isnan(sma20[m]) and close_arr[m] < sma20[m]
                if tp1_pct is None:
                    if pct <= STOP_LOSS_PCT:
                        exit_idx, pnl_pct, exit_reason = m, pct, "stop_loss"
                        break
                    if pct >= TP1_PCT:
                        tp1_pct = pct  # 1/3 매도, 잔여 2/3는 USAF 추세추종
                        continue
                    if ma20_break:
                        exit_idx, pnl_pct, exit_reason = m, pct, "ma20_break"
                        break
                else:
                    if signal_strong[m]:
                        blended = TP1_RATIO * tp1_pct + (1 - TP1_RATIO) * pct
                        exit_idx, pnl_pct, exit_reason = m, blended, "tp1_then_trend_exit"
                        break
                    if ma20_break:
                        blended = TP1_RATIO * tp1_pct + (1 - TP1_RATIO) * pct
                        exit_idx, pnl_pct, exit_reason = m, blended, "ma20_break"
                        break

            exit_date = exit_price = None
            hold_days = None
            if exit_idx is not None:
                exit_price = float(close_arr[exit_idx])
                exit_date = str(trading_dates[exit_idx])
                pnl_pct = round(pnl_pct, 2)
                hold_days = exit_idx - entry_idx
                cursor = exit_idx + 1
            else:
                exit_reason = "open"
                cursor = n

            rs_at_entry = rs_code[entry_idx]
            trades.append(dict(
                qualify_date=str(q_date),
                code=code,
                name=str(g["name"].iloc[entry_idx]),
                breakout_date=str(trading_dates[b_idx]),
                entry_date=str(trading_dates[entry_idx]),
                entry_price=round(entry_price),
                exit_date=exit_date,
                exit_price=round(exit_price) if exit_price is not None else None,
                exit_reason=exit_reason,
                pnl_pct=pnl_pct,
                hold_days=hold_days,
                rs_score=round(float(rs_at_entry), 1) if not np.isnan(rs_at_entry) else None,
            ))

    trades.sort(key=lambda t: (t["entry_date"], t["code"]))
    return dict(trades=_clean(trades), summary=_summarize(trades))

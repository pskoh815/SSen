# -*- coding: utf-8 -*-
"""§T 기준선 지지 + RS 추세 전략 — 트레이드 탭.

전략 로직:
  1. 후보 종목 풀: 최근 POOL_LOOKBACK_DAYS(60)일 이내에 아래 ⓐ 또는 ⓑ 중 1회 이상
     해당된 종목 (날짜별로 평가 — 신호일(N) 기준 최근 60일 내 1회라도 해당되면 후보)
       ⓐ 압도적 주도 테마일의 theme1_rank<=DOMINANT_TOP_RANK(2) 종목
       ⓑ 강세 주도종목/이벤트: 기여점수(contrib_score)>=POOL_CONTRIB_MIN(4) 종목
  2. 아래 5개 조건이 모두 충족되는 날(N)을 매수 신호일로 채택, 당일 종가에 매수:
       Condition1: 최근 NARROW_RANGE_BARS(5)봉 모두 (고가-저가)/저가*100 <= NARROW_RANGE_PCT(5%)
                   (최근 5봉 모두 일중 변동폭이 5% 이내인 좁은 가격대 → 변동성 수축 확인)
       Condition2: Lowest(L,15) > (Highest(H,60)+Lowest(L,60))/2
                   AND Lowest(L,20) > EMA(C,60)
       Condition3: EMA(C,60) > EMA(C,120) AND EMA(C,120) > EMA(C,240)
                   (장기 이평 정배열)
       Condition4: RS점수 ≥85 AND RS점수 > 코스피의 RS점수
       Condition5: 코스피 종가 > 코스피 EMA(20) AND 코스피 종가 > 코스피 EMA(60)
                   (시장 국면 필터: 코스피가 하락 추세일 때는 매수 신호 무효)
  3. 청산: 매수가 대비 종가 기준으로
       - -5% 이하 (1차 익절 전): 손절, 보유 전량 매도 (exit_reason="stop_loss")
       - +10% 이상 (1차 익절): 보유 물량의 1/3만 매도 (TP1_RATIO), 잔여 2/3는
         USAF v2.0(만능 매도 가속 감지 공식) 신호로 추세 추종
       - 1차 익절 이후, USAF >= USAF_THRESHOLD(0.5)인 날 잔여 2/3 전량 매도
         (exit_reason="tp1_then_trend_exit")
       기간 내 위 조건이 발생하지 않으면 exit_reason="open"
     pnl_pct는 1차 익절이 있을 경우 1차(1/3)/추세추종 청산(2/3) 수익률의 가중평균
     (TP1_RATIO : 1-TP1_RATIO)

USAF v2.0 (만능 매도 가속 감지 공식): RSI 모멘텀 붕괴(α) + 거래량 이상 팽창/분배(β)
+ 이동평균 이탈(γ) + 국면(횡보/급등) 가중(δ)을 0~1로 정규화해 가중합(α25%+β30%+γ20%+δ25%).
USAF >= 0.5이면 강한 매도 신호로 판정 (`_add_usaf_signal`)
  4. 포트폴리오 단위 계산: 동시 보유 가능 슬롯 PORTFOLIO_SLOTS개(슬롯당 비중
     1/PORTFOLIO_SLOTS)에 진입일 순서로 거래를 배정. 빈 슬롯이 없으면 해당
     거래는 매수 자금 부족으로 결과에서 제외. 각 슬롯은 청산 시 자기 자본을
     (1+pnl_pct/100)배로 복리 갱신하며, 매도 이벤트 순서의 슬롯 합산 자본으로
     total_pnl_pct/MDD를 계산

RS점수: 20/60/120/250거래일 수익률을 종목별로 계산 후 해당일 기준 전체 종목(+코스피) 대비
백분위(rank pct)로 환산, 가중합 (20일 10% / 60일 30% / 120일 20% / 250일 40%,
docs/kkangto_spec.md RS 정의와 동일)

데이터 소스: data/market/ohlcv (data.go.kr 수집 일별 OHLCV, 전 종목) + fact_kospi
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from .period_analysis import _clean, _load, _load_kospi, api_period_dominant_days
from .perf_timer import timed_db_query

_ROOT   = Path(__file__).resolve().parents[3]
_FOHLCV = str(_ROOT / "data" / "market" / "ohlcv" / "**" / "*.parquet").replace("\\", "/")

ST_PERIOD       = 21    # 슈퍼트렌드 ATR 기간
ST_MULTI        = 3.0   # 슈퍼트렌드 ATR 배수
HIGH52W_WINDOW  = 252   # 52주 신고가 산출 윈도우(거래일)
NEAR_HIGH_PCT   = 15.0  # 52주 신고가 대비 허용 괴리율(%)
BREAKOUT_WINDOW = 3     # 첫 돌파 후 단봉 탐색 기간(거래일)
NARROW_BODY_PCT = 3.0   # 단봉 기준: |종가-시가|/시가 (%)

# ── Condition1~4 파라미터 ─────────────────────────────────────────────────────
NARROW_RANGE_BARS = 5    # Condition1: 최근 N봉 모두 일중 변동폭 좁음 확인
NARROW_RANGE_PCT  = 5.0  # (고가-저가)/저가*100 <= 이 값(%)
LOW15_WINDOW    = 15    # Lowest(L,15)
HL60_WINDOW     = 60    # Highest(H,60) / Lowest(L,60)
LOW20_WINDOW    = 20    # Lowest(L,20)
EMA_TREND_PERIODS = (60, 120, 240)  # EMA(C,60/120/240) 정배열

# ── 청산(매도) 파라미터: 손절/1차 익절/추세추종 ────────────────────────────────
STOP_LOSS_PCT = -5.0   # 매수가 대비 -5% 손절 (1차 익절 전)
TP1_PCT       = 10.0   # 매수가 대비 +10% 이상 시 1차 익절
TP1_RATIO     = 1.0 / 3.0  # 1차 익절 매도 비율(1/3), 잔여 2/3는 USAF 추세추종

# ── USAF v2.0 (만능 매도 가속 감지 공식) 파라미터: 1차 익절 후 잔여 물량 추세추종 ──
USAF_RSI_PERIOD = 14    # RSI 기간
USAF_THRESHOLD  = 0.5   # USAF >= 이 값이면 강한 매도 신호(SIGNAL_STRONG)

# ── RS점수(상대강도) 파라미터 (docs/kkangto_spec.md) ───────────────────────────
RS_WINDOWS = {20: 0.10, 60: 0.30, 120: 0.20, 250: 0.40}  # 기간별 가중치
RS_MIN     = 85.0   # RS점수 하한
KOSPI_COL  = "__KOSPI__"  # RS 매트릭스 내 코스피 지수용 가상 컬럼명

# ── 시장 국면 필터 파라미터 ───────────────────────────────────────────────────
MARKET_EMA_PERIODS = (20, 60)  # 코스피 종가 > 코스피 EMA(20) AND 코스피 종가 > 코스피 EMA(60)일 때만 매수 신호 허용

# ── 후보 종목 풀 파라미터 (압도적 주도 테마 / 강세 주도종목·이벤트) ──────────────
DOMINANT_TOP_RANK  = 2    # 압도적 주도 테마일의 theme1_rank<=N 종목을 후보로 인정
POOL_CONTRIB_MIN   = 4.0  # 강세 주도종목/이벤트 판정 기여점수 기준("주도" 버킷, api_period_breadth)
POOL_LOOKBACK_DAYS = 60   # 후보 풀 판정 룩백 기간(일): 최근 N일 내 1회 이상 해당 시 후보로 인정

# ── 포트폴리오 단위 계산 파라미터 ─────────────────────────────────────────────
PORTFOLIO_SLOTS = 10    # 동시 보유 가능 슬롯 수 (슬롯당 비중 1/PORTFOLIO_SLOTS)

MIN_BARS = max(EMA_TREND_PERIODS) + 10  # 지표 워밍업에 필요한 최소 봉 수


def _load_ohlcv(codes: list[str], start: date, end: date) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    code_list = ",".join(f"'{c}'" for c in codes)
    sql = f"""
        SELECT date, code, name, open, high, low, close, volume, amount
        FROM   read_parquet('{_FOHLCV}', hive_partitioning=true)
        WHERE  code IN ({code_list})
          AND  date BETWEEN '{start}' AND '{end}'
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


def api_stock_ohlcv(code: str, end: date, bars: int) -> pd.DataFrame:
    """단일 종목의 end일 기준 최근 bars개 봉 OHLCV (date 오름차순)."""
    sql = f"""
        SELECT date, name, open, high, low, close, volume
        FROM   read_parquet('{_FOHLCV}', hive_partitioning=true)
        WHERE  code = ? AND date <= ?
        ORDER  BY date DESC
        LIMIT  ?
    """
    con = duckdb.connect()
    try:
        with timed_db_query():
            df = con.execute(sql, [code, end, bars]).fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.sort_values("date").reset_index(drop=True)


def _load_all_ohlcv(start: date, end: date) -> pd.DataFrame:
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


def _atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder's ATR: 첫 값은 단순평균, 이후 (prev*(n-1)+TR)/n."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    n = len(tr)
    atr = np.full(n, np.nan)
    if n < period:
        return pd.Series(atr, index=tr.index)
    tr_np = tr.to_numpy()
    atr[period - 1] = tr_np[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr_np[i]) / period
    return pd.Series(atr, index=tr.index)


def _supertrend(g: pd.DataFrame, period: int = ST_PERIOD, multiplier: float = ST_MULTI) -> pd.DataFrame:
    """단일 종목 OHLC DataFrame(date 오름차순)에 슈퍼트렌드 컬럼 추가.

    EasyLanguage 정의:
      Src=(H+L)/2, BasicUp=Src+Multi*ATR, BasicDn=Src-Multi*ATR
      FinalUp = BasicUp if (BasicUp<FinalUp[1] or Close[1]>FinalUp[1]) else FinalUp[1]
      FinalDn = BasicDn if (BasicDn>FinalDn[1] or Close[1]<FinalDn[1]) else FinalDn[1]
      Trend = -1 if (Trend[1]==1 and Close<FinalDn) else (1 if (Trend[1]==-1 and Close>FinalUp) else Trend[1])
      SuperTrend = FinalDn if Trend==1 else FinalUp
    """
    g = g.reset_index(drop=True)
    high, low, close = g["high"], g["low"], g["close"]
    src = (high + low) / 2
    atr = _atr_wilder(high, low, close, period)
    basic_up = (src + multiplier * atr).to_numpy()
    basic_dn = (src - multiplier * atr).to_numpy()
    close_np = close.to_numpy()

    n = len(g)
    final_up = np.full(n, np.nan)
    final_dn = np.full(n, np.nan)
    trend = np.full(n, np.nan)
    supertrend = np.full(n, np.nan)

    start_i = period - 1  # 첫 ATR 유효 인덱스
    for i in range(start_i, n):
        bu, bd = basic_up[i], basic_dn[i]
        if i == start_i:
            final_up[i] = bu
            final_dn[i] = bd
            trend[i] = 1
        else:
            pf_up, pf_dn = final_up[i - 1], final_dn[i - 1]
            prev_close = close_np[i - 1]
            final_up[i] = bu if (bu < pf_up or prev_close > pf_up) else pf_up
            final_dn[i] = bd if (bd > pf_dn or prev_close < pf_dn) else pf_dn

            prev_trend = trend[i - 1]
            c = close_np[i]
            if prev_trend == 1 and c < final_dn[i]:
                trend[i] = -1
            elif prev_trend == -1 and c > final_up[i]:
                trend[i] = 1
            else:
                trend[i] = prev_trend

        supertrend[i] = final_dn[i] if trend[i] == 1 else final_up[i]

    g["atr"] = atr.values
    g["supertrend"] = supertrend
    g["trend"] = trend
    return g


def _add_52w_high(g: pd.DataFrame, window: int = HIGH52W_WINDOW) -> pd.DataFrame:
    g["high52w"] = g["high"].rolling(window, min_periods=1).max()
    return g


def _add_usaf_signal(g: pd.DataFrame) -> pd.DataFrame:
    """USAF v2.0(만능 매도 가속 감지 공식): RSI 모멘텀 붕괴(α) + 거래량 이상 팽창(β)
    + 이동평균 이탈(γ) + 국면 가중(δ)을 정규화해 가중합. USAF>=USAF_THRESHOLD인
    날 signal_strong=True (1차 익절 후 잔여 물량 추세추종 청산 신호)."""
    high, low, close, volume = g["high"], g["low"], g["close"], g["volume"]

    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    vol_ma20 = volume.rolling(20).mean()

    # α: RSI 모멘텀 붕괴 계수 (3봉/1봉 하락 강도, 최솟값 0.1 보장)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / USAF_RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / USAF_RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi14 = (100 - 100 / (1 + rs)).fillna(100)

    rsi_prev3 = rsi14.shift(3)
    rsi_prev1 = rsi14.shift(1)
    rsi_drop3 = np.where(rsi14 < rsi_prev3, (rsi_prev3 - rsi14) / 100, 0.0)
    rsi_drop1 = np.where(rsi14 < rsi_prev1, (rsi_prev1 - rsi14) / 100, 0.0)
    alpha = np.maximum(rsi_drop3 * 0.7 + rsi_drop1 * 0.3, 0.1)

    # β: 거래량 이상 팽창 계수 (분배 패턴 / 급등 천장 패턴)
    vol_ratio = volume / vol_ma20
    price_chg = close.pct_change(fill_method=None) * 100
    beta_dist = np.where((vol_ratio > 1.5) & (price_chg < 1.5), vol_ratio, 1.0)
    beta_top = np.where((vol_ratio > 2.0) & (price_chg > 3.0), vol_ratio * 0.8, 1.0)
    beta = np.maximum(beta_top, beta_dist)

    # γ: 이동평균 이탈 계수
    ma_gap = (ma5 - ma20) / ma20 * 100
    gamma = np.where(ma5 < ma20, ma_gap.abs(), ma_gap * 0.2)

    # δ: 국면(횡보/급등) 자동 판별 가중치
    high20 = high.rolling(20).max()
    low20 = low.rolling(20).min()
    range_pct = np.where(low20 > 0, (high20 - low20) / low20 * 100, 0.0)
    dev_ma60 = np.where(ma60 > 0, (close - ma60) / ma60 * 100, 0.0)
    delta_side = np.where(range_pct < 5, 2.5, np.where(range_pct < 10, 1.8, 1.0))
    delta_top = np.where(dev_ma60 > 30, 2.5,
                          np.where(dev_ma60 > 20, 2.0,
                                   np.where(dev_ma60 > 15, 1.5, 1.0)))
    delta_coef = np.maximum(delta_top, delta_side)

    # 정규화 후 가중합 (α25% + β30% + γ20% + δ25%)
    alpha_n = np.minimum(alpha, 1.0)
    beta_n = np.minimum(beta / 3, 1.0)
    gamma_n = np.minimum(gamma / 10, 1.0)
    delta_n = np.clip((delta_coef - 1) / 1.5, 0.0, 1.0)

    usaf = alpha_n * 0.25 + beta_n * 0.30 + gamma_n * 0.20 + delta_n * 0.25
    g["signal_strong"] = usaf >= USAF_THRESHOLD
    return g


def _compute_signal_indicators_wide(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, volume: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Condition1~3 + USAF 신호를 종목 행렬 전체에서 한 번에 벡터화 계산.

    기존에 종목별로 _add_usaf_signal 등을 반복 호출해 산출하던 것과 동치
    (상장 전 leading-NaN만 있는 종목은 rolling/ewm이 dropna 시리즈와 동일한 값을
    내므로 reindex 후 per-code 슬라이싱 결과가 같음 — RS행렬(cond4)도 같은 방식)."""
    range_pct = (high - low) / low * 100
    cond1 = range_pct.rolling(NARROW_RANGE_BARS).max() <= NARROW_RANGE_PCT

    low15  = low.rolling(LOW15_WINDOW).min()
    high60 = high.rolling(HL60_WINDOW).max()
    low60  = low.rolling(HL60_WINDOW).min()
    mid60  = (high60 + low60) / 2
    low20  = low.rolling(LOW20_WINDOW).min()
    ema60  = close.ewm(span=EMA_TREND_PERIODS[0], adjust=False).mean()
    ema120 = close.ewm(span=EMA_TREND_PERIODS[1], adjust=False).mean()
    ema240 = close.ewm(span=EMA_TREND_PERIODS[2], adjust=False).mean()
    cond2 = (low15 > mid60) & (low20 > ema60)
    cond3 = (ema60 > ema120) & (ema120 > ema240)

    ma5  = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    vol_ma20 = volume.rolling(20).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / USAF_RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / USAF_RSI_PERIOD, adjust=False).mean()
    rs_rsi = avg_gain / avg_loss.replace(0, np.nan)
    rsi14 = (100 - 100 / (1 + rs_rsi)).fillna(100)

    rsi_prev3 = rsi14.shift(3)
    rsi_prev1 = rsi14.shift(1)
    rsi_drop3 = np.where((rsi14 < rsi_prev3).to_numpy(), ((rsi_prev3 - rsi14) / 100).to_numpy(), 0.0)
    rsi_drop1 = np.where((rsi14 < rsi_prev1).to_numpy(), ((rsi_prev1 - rsi14) / 100).to_numpy(), 0.0)
    alpha = np.maximum(rsi_drop3 * 0.7 + rsi_drop1 * 0.3, 0.1)

    vol_ratio = (volume / vol_ma20).to_numpy()
    price_chg = (close.pct_change(fill_method=None) * 100).to_numpy()
    beta_dist = np.where((vol_ratio > 1.5) & (price_chg < 1.5), vol_ratio, 1.0)
    beta_top  = np.where((vol_ratio > 2.0) & (price_chg > 3.0), vol_ratio * 0.8, 1.0)
    beta = np.maximum(beta_top, beta_dist)

    ma_gap = ((ma5 - ma20) / ma20 * 100).to_numpy()
    gamma = np.where((ma5 < ma20).to_numpy(), np.abs(ma_gap), ma_gap * 0.2)

    high20 = high.rolling(20).max()
    dev_ma60    = np.where((ma60  > 0).to_numpy(), ((close - ma60) / ma60 * 100).to_numpy(), 0.0)
    range_pct20 = np.where((low20 > 0).to_numpy(), ((high20 - low20) / low20 * 100).to_numpy(), 0.0)
    delta_side = np.where(range_pct20 < 5, 2.5, np.where(range_pct20 < 10, 1.8, 1.0))
    delta_top  = np.where(dev_ma60 > 30, 2.5,
                          np.where(dev_ma60 > 20, 2.0,
                                   np.where(dev_ma60 > 15, 1.5, 1.0)))
    delta_coef = np.maximum(delta_top, delta_side)

    alpha_n = np.minimum(alpha, 1.0)
    beta_n  = np.minimum(beta / 3, 1.0)
    gamma_n = np.minimum(gamma / 10, 1.0)
    delta_n = np.clip((delta_coef - 1) / 1.5, 0.0, 1.0)
    usaf = alpha_n * 0.25 + beta_n * 0.30 + gamma_n * 0.20 + delta_n * 0.25
    signal_strong = pd.DataFrame(usaf >= USAF_THRESHOLD, index=close.index, columns=close.columns)

    return cond1, cond2, cond3, signal_strong


def _compute_rs(close: pd.DataFrame) -> pd.DataFrame:
    """RS점수(0~100): 기간별 수익률의 횡단면 백분위 가중합 (docs/kkangto_spec.md)."""
    rs = None
    for window, weight in RS_WINDOWS.items():
        pct = close.pct_change(window, fill_method=None)
        rank_pct = pct.rank(axis=1, pct=True) * 100
        term = rank_pct * weight
        rs = term if rs is None else rs + term
    return rs


def _empty_summary() -> dict[str, Any]:
    return dict(total_trades=0, closed_trades=0, win_rate_pct=None,
                 avg_pnl_pct=None, total_pnl_pct=None, max_drawdown_pct=None)


def _summarize(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return _empty_summary()

    closed = [t for t in trades if t["pnl_pct"] is not None]
    total = len(trades)
    if not closed:
        return dict(total_trades=total, closed_trades=0, win_rate_pct=None,
                     avg_pnl_pct=None, total_pnl_pct=None, max_drawdown_pct=None)

    closed_sorted = sorted(closed, key=lambda t: t["entry_date"])
    pnls = pd.Series([t["pnl_pct"] for t in closed_sorted])
    wins = int((pnls > 0).sum())

    cumret = (1 + pnls / 100).cumprod()
    running_max = cumret.cummax()
    drawdown = (cumret - running_max) / running_max * 100

    return dict(
        total_trades=total,
        closed_trades=len(closed),
        win_rate_pct=round(wins / len(closed) * 100, 2),
        avg_pnl_pct=round(float(pnls.mean()), 2),
        total_pnl_pct=round(float((cumret.iloc[-1] - 1) * 100), 2),
        max_drawdown_pct=round(float(drawdown.min()), 2),
    )


def _portfolio_summarize(trades: list[dict], slots: int = PORTFOLIO_SLOTS
                          ) -> tuple[list[dict], dict[str, Any]]:
    """포트폴리오 단위 계산: 동시 보유 가능한 `slots`개 슬롯(슬롯당 비중 1/slots)에
    진입일 순서로 거래를 배정하고, 빈 슬롯이 없으면 해당 거래는 매수 자금 부족으로 제외.
    각 슬롯은 청산 시점마다 자기 자본을 (1+pnl_pct/100)배로 복리 갱신하며,
    매도 이벤트 순서로 슬롯 합산 자본의 등락을 추적해 MDD를 계산."""
    if not trades:
        return [], _empty_summary()

    weight = 1.0 / slots
    free_at: list[str | None] = [None] * slots  # 슬롯이 다음 진입을 받을 수 있는 날짜(이 날짜 이후)
    taken: list[dict] = []

    for t in sorted(trades, key=lambda x: x["entry_date"]):
        slot_idx = None
        for i in range(slots):
            if free_at[i] is None or free_at[i] <= t["entry_date"]:
                slot_idx = i
                break
        if slot_idx is None:
            continue  # 가용 슬롯 없음 → 자금 부족으로 매수 불가
        free_at[slot_idx] = t["exit_date"] if t["exit_date"] is not None else "9999-99-99"
        t = dict(t, _slot=slot_idx)
        taken.append(t)

    closed = [t for t in taken if t["pnl_pct"] is not None]
    total = len(taken)
    if not closed:
        for t in taken:
            t.pop("_slot", None)
        return taken, dict(total_trades=total, closed_trades=0, win_rate_pct=None,
                            avg_pnl_pct=None, total_pnl_pct=None, max_drawdown_pct=None)

    closed_sorted = sorted(closed, key=lambda t: t["exit_date"])
    equity = [weight] * slots
    curve: list[float] = []
    for t in closed_sorted:
        equity[t["_slot"]] *= (1 + t["pnl_pct"] / 100)
        curve.append(sum(equity))

    curve_s = pd.Series(curve)
    running_max = curve_s.cummax()
    drawdown = (curve_s - running_max) / running_max * 100

    pnls = pd.Series([t["pnl_pct"] for t in closed_sorted])
    wins = int((pnls > 0).sum())

    for t in taken:
        t.pop("_slot", None)

    summary = dict(
        total_trades=total,
        closed_trades=len(closed),
        win_rate_pct=round(wins / len(closed) * 100, 2),
        avg_pnl_pct=round(float(pnls.mean()), 2),
        total_pnl_pct=round(float((curve_s.iloc[-1] - 1.0) * 100), 2),
        max_drawdown_pct=round(float(drawdown.min()), 2),
    )
    return taken, summary


def api_supertrend_trades(start: date, end: date) -> dict[str, Any]:
    """§T 슈퍼트렌드 돌파 + RS 추세 전략 백테스트 (전 종목 대상)."""
    # 지표 계산용 OHLCV: RS(250)/EMA(240) 워밍업 버퍼 확보
    rs_buffer_days = int(max(max(RS_WINDOWS), max(EMA_TREND_PERIODS)) * 1.6)
    load_start = start - timedelta(days=rs_buffer_days)
    load_end = end + timedelta(days=60)
    ohlcv = _load_all_ohlcv(load_start, load_end)
    if ohlcv.empty:
        return dict(trades=[], summary=_empty_summary())

    close_mat = ohlcv.pivot(index="date", columns="code", values="close").sort_index()
    kospi = _load_kospi(load_start, load_end)
    kospi["date"] = kospi["date"].dt.date
    kospi_s = kospi.set_index("date")["close"].reindex(close_mat.index).ffill().bfill()
    close_mat[KOSPI_COL] = kospi_s
    rs_mat = _compute_rs(close_mat)

    high_mat = ohlcv.pivot(index="date", columns="code", values="high").sort_index()
    low_mat = ohlcv.pivot(index="date", columns="code", values="low").sort_index()
    volume_mat = ohlcv.pivot(index="date", columns="code", values="volume").sort_index()

    # ── 시장 국면 필터: 코스피 종가가 코스피 EMA(20)·EMA(60) 위일 때만 매수 신호 허용
    market_uptrend = pd.Series(True, index=kospi_s.index)
    for period in MARKET_EMA_PERIODS:
        kospi_ema = kospi_s.ewm(span=period, adjust=False).mean()
        market_uptrend &= kospi_s > kospi_ema

    # ── 후보 종목 풀
    #    ⓐ 압도적 주도 테마(theme1_rank<=DOMINANT_TOP_RANK)
    #    ⓑ 강세 주도종목/이벤트(기여점수>=POOL_CONTRIB_MIN)
    #    위 ⓐ/ⓑ에 최근 POOL_LOOKBACK_DAYS일 이내 1회 이상 해당된 종목만
    #    신호 탐색 대상으로 인정 (날짜별 판정)
    pool_df = _load(load_start, load_end,
                     ["date", "code", "theme1", "theme1_rank", "contrib_score"])
    pool_df["date"] = pd.to_datetime(pool_df["date"])

    # ⓐ 압도적 주도 테마 theme1_rank<=N
    dom = api_period_dominant_days(load_start, load_end)
    dominant_theme_by_date = {pd.Timestamp(d["date"]): d["theme1"] for d in dom["days"]}
    dom_theme = pool_df["date"].map(dominant_theme_by_date)
    is_dominant_top2 = (pool_df["theme1"] == dom_theme) & (pool_df["theme1_rank"] <= DOMINANT_TOP_RANK)

    # ⓑ 기여점수>=POOL_CONTRIB_MIN 종목
    qual_flag = pool_df["contrib_score"] >= POOL_CONTRIB_MIN

    pool_df["today_qual"] = is_dominant_top2.fillna(False) | qual_flag

    pool_wide = (pool_df.pivot(index="date", columns="code", values="today_qual")
                         .astype("boolean").fillna(False).astype(bool).sort_index())
    pool_ok_mat = pool_wide.astype(int).rolling(f"{POOL_LOOKBACK_DAYS}D").max().astype(bool)
    qualifying_codes = set(pool_wide.columns[pool_wide.any(axis=0)])
    if not qualifying_codes:
        return dict(trades=[], summary=_empty_summary())

    # ── Condition1~3 + USAF 신호: 후보 종목 전체를 한 번에 벡터화 계산 (종목별 반복 제거)
    calc_codes = [c for c in qualifying_codes if c in close_mat.columns]
    cond1_mat, cond2_mat, cond3_mat, signal_strong_mat = _compute_signal_indicators_wide(
        close_mat[calc_codes], high_mat[calc_codes], low_mat[calc_codes], volume_mat[calc_codes]
    )

    trades: list[dict] = []
    for code, g in ohlcv.groupby("code"):
        if code not in qualifying_codes:
            continue
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < MIN_BARS:
            continue

        trading_dates = g["date"].tolist()
        n = len(g)

        close = g["close"].values
        signal_strong = signal_strong_mat[code].reindex(trading_dates).to_numpy()

        rs_code = rs_mat[code].reindex(trading_dates).values
        rs_kospi = rs_mat[KOSPI_COL].reindex(trading_dates).values
        market_ok = market_uptrend.reindex(trading_dates).values

        if code in pool_ok_mat.columns:
            pool_ok = pool_ok_mat[code].reindex(pd.to_datetime(trading_dates), fill_value=False).to_numpy()
        else:
            pool_ok = np.zeros(n, dtype=bool)

        # ── Condition1~5: 변동성 수축(5봉 변동폭<=5%) + 저점/이평 추세 + RS 상대강도 + 시장 국면
        cond1 = cond1_mat[code].reindex(trading_dates).to_numpy()
        cond2 = cond2_mat[code].reindex(trading_dates).to_numpy()
        cond3 = cond3_mat[code].reindex(trading_dates).to_numpy()
        cond4 = (rs_code >= RS_MIN) & (rs_code > rs_kospi)
        cond5_market = (cond1 & cond2 & cond3 & cond4 & market_ok)

        signal_mask = cond5_market & pool_ok

        signal_indices = [i for i in range(n) if signal_mask[i]]
        if not signal_indices:
            continue

        cursor = 0
        for sig in signal_indices:
            if sig < cursor:
                continue

            # ── 신호일(N): 기준선 지지를 확인한 당일 종가에 매수
            entry_idx = sig
            entry_price = float(close[entry_idx])

            # ── 청산: 매수가 대비 -5% 손절(1차 익절 전) / +10% 1차 익절(1/3 매도)
            #         후 잔여 2/3는 USAF>=USAF_THRESHOLD인 날 전량 매도
            exit_idx = exit_date = exit_price = exit_reason = None
            pnl_pct = hold_days = None
            tp1_pct = None  # 1차(1/3) 매도 시점의 수익률
            for m in range(entry_idx + 1, n):
                pct = float((close[m] - entry_price) / entry_price * 100)
                if tp1_pct is None:
                    if pct <= STOP_LOSS_PCT:
                        exit_idx, pnl_pct, exit_reason = m, pct, "stop_loss"
                        break
                    if pct >= TP1_PCT:
                        tp1_pct = pct  # 1/3 매도, 잔여 2/3는 USAF 추세추종
                else:
                    if signal_strong[m]:
                        blended = TP1_RATIO * tp1_pct + (1 - TP1_RATIO) * pct
                        exit_idx, pnl_pct, exit_reason = m, blended, "tp1_then_trend_exit"
                        break

            if exit_idx is not None:
                exit_price = float(close[exit_idx])
                exit_date = str(trading_dates[exit_idx])
                pnl_pct = round(pnl_pct, 2)
                hold_days = exit_idx - entry_idx
                cursor = exit_idx + 1
            else:
                exit_reason = "open"
                cursor = n

            sig_date = trading_dates[sig]
            if not (start <= sig_date <= end):
                continue

            rs_at_entry = rs_code[entry_idx]
            trades.append(dict(
                code=code,
                name=str(g["name"].iloc[sig]),
                breakout_date=str(trading_dates[sig]),
                signal_date=str(trading_dates[sig]),
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
    taken, summary = _portfolio_summarize(trades)
    return dict(trades=_clean(taken), summary=summary)

"""Pydantic 응답 스키마 (E4). api_version 포함 — 스키마 변경 시 버전 올림."""
from __future__ import annotations
from datetime import date, datetime
from typing import Any, Optional
from pydantic import BaseModel, Field

API_VERSION = "1.0"


class Meta(BaseModel):
    api_version: str = Field(default=API_VERSION)
    dataset_version: str
    generated_at: datetime


class DatasetInfo(BaseModel):
    dataset_version: str
    last_updated_at: Optional[str]
    min_date: Optional[date]
    max_date: Optional[date]
    source_file: Optional[str]


class DatasetResponse(BaseModel):
    meta: Meta
    data: DatasetInfo


# ── /leaders/daily ─────────────────────────────────────────────────────────

class DailyLeader(BaseModel):
    date: date
    theme1: str
    theme_amount: Optional[int]
    leader_code: Optional[str]
    leader_name: Optional[str]
    leader_rank: Optional[int]
    leader_close: Optional[int]
    avg_change_pct: Optional[float]
    stock_count: Optional[int]


class DailyLeaderResponse(BaseModel):
    meta: Meta
    data: list[DailyLeader]


# ── /leaders/regimes ────────────────────────────────────────────────────────

class Regime(BaseModel):
    regime_id: int
    theme1: str
    leader_code: Optional[str]
    leader_name: Optional[str]
    start_date: date
    end_date: date
    duration_days: int
    avg_theme_amount: Optional[int]


class RegimesResponse(BaseModel):
    meta: Meta
    total: int
    data: list[Regime]


# ── /trades ─────────────────────────────────────────────────────────────────

class Trade(BaseModel):
    trade_id: int
    regime_id: Optional[int]
    code: str
    name: Optional[str]
    theme1: Optional[str]
    signal_date: date
    entry_date: date
    entry_price: Optional[int]
    exit_date: Optional[date]
    exit_price: Optional[int]
    exit_reason: Optional[str]
    pnl_pct: Optional[float]
    fee_pct: Optional[float]
    net_pnl_pct: Optional[float]
    hold_days: Optional[int]


class TradesResponse(BaseModel):
    meta: Meta
    total: int
    summary: TradeSummary
    data: list[Trade]
    notice: Optional[list[str]] = None


class TradeSummary(BaseModel):
    total_trades: int
    closed_trades: int
    win_rate_pct: Optional[float]
    avg_net_pnl_pct: Optional[float]
    total_net_pnl_pct: Optional[float]
    max_drawdown_pct: Optional[float]
    avg_hold_days: Optional[float]


# ── /stocks/{code}/summary ───────────────────────────────────────────────────

class StockSummary(BaseModel):
    code: str
    name: Optional[str]
    appear_days: int
    avg_rank: Optional[float]
    avg_change_pct: Optional[float]
    top_theme1: Optional[str]
    best_pnl_pct: Optional[float]
    worst_pnl_pct: Optional[float]
    trade_count: int


class StockSummaryResponse(BaseModel):
    meta: Meta
    data: StockSummary


# ── /api/stocks/{code}/ohlcv ─────────────────────────────────────────────────

class StockOhlcvBar(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class StockOhlcvResponse(BaseModel):
    meta: Meta
    code: str
    name: Optional[str]
    data: list[StockOhlcvBar]


# ── /themes/{theme}/summary ──────────────────────────────────────────────────

class ThemeSummary(BaseModel):
    theme1: str
    regime_count: int
    total_duration_days: int
    avg_duration_days: Optional[float]
    trade_count: int
    win_rate_pct: Optional[float]
    avg_net_pnl_pct: Optional[float]
    top_leader_code: Optional[str]
    top_leader_name: Optional[str]


class ThemeSummaryResponse(BaseModel):
    meta: Meta
    data: ThemeSummary


# ── /perf ───────────────────────────────────────────────────────────────────

class PerfResult(BaseModel):
    endpoint: str
    n_samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


class PerfResponse(BaseModel):
    meta: Meta
    results: list[PerfResult]


# ── /api/period/* (E8) ──────────────────────────────────────────────────────

class PeriodTopStock(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    cumret_pct: Optional[float] = None


class PeriodThemeItem(BaseModel):
    theme1: str
    appear_days: int
    cumret_pct: float
    cumret_first: Optional[float]   # 1거래일 폴백 시 전반/후반 정의 불가 → None
    cumret_second: Optional[float]  # 1거래일 폴백 시 전반/후반 정의 불가 → None
    total_amount: float
    up_ratio: float
    composite: float
    rotation: Optional[float]       # 1거래일 폴백 시 로테이션 정의 불가 → None
    fall_score: Optional[float]
    top_stock_code: Optional[str] = None
    top_stock_name: Optional[str] = None
    top_stock_cumret_pct: Optional[float] = None
    top_stocks: list[PeriodTopStock] = []  # 테마 내 1~3위 종목 (상승 테마=수익률 상위, 하락 테마=하위)


class PeriodThemesResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    rising: list[PeriodThemeItem]
    falling: list[PeriodThemeItem]
    rotating: list[PeriodThemeItem]
    all: list[PeriodThemeItem]
    notice: Optional[list[str]] = None


class ThemeTrendSeries(BaseModel):
    theme1: str
    cum: list[float]


class PeriodThemeTrendResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    dates: list[str]
    series: list[ThemeTrendSeries]
    kospi: list[float]
    notice: Optional[list[str]] = None


class PeriodDominantDay(BaseModel):
    date: str
    theme1: str
    theme1_pct: float
    n_stocks: int
    theme1_amount: float
    amount_ratio: Optional[float]
    pct_gap: float


class PeriodDominantDaysResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    days: list[PeriodDominantDay]
    total: int
    notice: Optional[list[str]] = None


class PeriodThemeRankDay(BaseModel):
    date: str
    theme1: str
    theme1_pct: float
    n_stocks: int
    theme1_amount: float
    amount_ratio: Optional[float]
    pct_gap: float


class PeriodThemeRankDaysResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    days: list[PeriodThemeRankDay]
    total: int
    notice: Optional[list[str]] = None


class PeriodDominantStock(BaseModel):
    date: str
    code: str
    name: str
    theme1_rank: int
    size_class: Optional[str]
    theme1: str
    change_pct: Optional[float]
    amount_100b: Optional[float]
    rs: Optional[float] = None


class PeriodDominantTopStocksResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    stocks: list[PeriodDominantStock]
    total: int
    notice: Optional[list[str]] = None


class PeriodLeaderItem(BaseModel):
    code: str
    name: str
    theme1: str
    appear_days: int
    coverage_pct: float
    max_score: float
    avg_score: float
    recent3_avg: float
    cumret_pct: float
    rank_momentum: float
    composite: float
    first_signal_date: Optional[str]
    first_signal_rs: Optional[float] = None


class PeriodLeadersResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    total: int
    leaders: list[PeriodLeaderItem]
    notice: Optional[list[str]] = None


class BucketCounts(BaseModel):
    주도: int
    강세: int
    중립: int
    약세: int
    total: int
    bull_ratio: float


class PeriodBreadthResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    by_size: dict[str, BucketCounts]
    by_market: dict[str, BucketCounts]
    notice: Optional[list[str]] = None


class PeriodEventItem(BaseModel):
    date: str
    code: str
    name: Optional[str]
    theme1: Optional[str]
    contrib_score: float
    cum_contrib: float
    theme1_rank: Optional[float]
    rs: Optional[float] = None


class PeriodEventsResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    total: int
    events: list[PeriodEventItem]
    notice: Optional[list[str]] = None


# ── /api/period/supertrend-trades (E9) ───────────────────────────────────────

class SuperTrendTrade(BaseModel):
    code: str
    name: str
    breakout_date: str
    signal_date: str
    entry_date: str
    entry_price: Optional[int]
    exit_date: Optional[str]
    exit_price: Optional[int]
    exit_reason: Optional[str]
    pnl_pct: Optional[float]
    hold_days: Optional[int]
    rs_score: Optional[float] = None


class SuperTrendSummary(BaseModel):
    total_trades: int
    closed_trades: int
    win_rate_pct: Optional[float]
    avg_pnl_pct: Optional[float]
    total_pnl_pct: Optional[float]
    max_drawdown_pct: Optional[float]


class SuperTrendTradesResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    summary: SuperTrendSummary
    trades: list[SuperTrendTrade]
    notice: Optional[list[str]] = None


# ── /api/period/rs-breakout-trades (E10) ─────────────────────────────────────

class RsBreakoutTrade(BaseModel):
    qualify_date: str
    code: str
    name: str
    breakout_date: str
    entry_date: str
    entry_price: Optional[int]
    exit_date: Optional[str]
    exit_price: Optional[int]
    exit_reason: Optional[str]
    pnl_pct: Optional[float]
    hold_days: Optional[int]
    rs_score: Optional[float] = None


class RsBreakoutSummary(BaseModel):
    total_trades: int
    closed_trades: int
    win_rate_pct: Optional[float]
    avg_pnl_pct: Optional[float]
    total_pnl_pct: Optional[float]
    max_drawdown_pct: Optional[float]


class RsBreakoutTradesResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    summary: RsBreakoutSummary
    trades: list[RsBreakoutTrade]
    notice: Optional[list[str]] = None


# ── /api/period/pullback-trades (E11) ────────────────────────────────────────

class PullbackTrade(BaseModel):
    code: str
    name: str
    breakout_date: str
    signal_date: str
    entry_date: str
    entry_price: Optional[int]
    exit_date: Optional[str]
    exit_price: Optional[int]
    exit_reason: Optional[str]
    pnl_pct: Optional[float]
    hold_days: Optional[int]


class PullbackSummary(BaseModel):
    total_trades: int
    closed_trades: int
    win_rate_pct: Optional[float]
    avg_pnl_pct: Optional[float]
    total_pnl_pct: Optional[float]
    max_drawdown_pct: Optional[float]


class PullbackTradesResponse(BaseModel):
    meta: Meta
    start: date
    end: date
    summary: PullbackSummary
    trades: list[PullbackTrade]
    notice: Optional[list[str]] = None

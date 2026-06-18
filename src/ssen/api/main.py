"""
E4+E6: FastAPI 서버
Usage: uvicorn ssen.api.main:app --host 0.0.0.0 --port 8000 --reload

Swagger UI: http://localhost:8000/docs
대시보드:   http://localhost:8000/dashboard
"""
from __future__ import annotations

import logging
import logging.config
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path as _Path

from . import cache as _cache_mod
from . import db
from .middleware import LoggingMetricsMiddleware, get_metrics
from .scheduler import (init_scheduler, shutdown_scheduler, trigger_e3_now,
                        trigger_cache_warmup_now, trigger_daily_collect_now)
from .schemas import (
    API_VERSION, DatasetInfo, DatasetResponse, DailyLeader, DailyLeaderResponse,
    Meta, PerfResponse, PerfResult, Regime, RegimesResponse, StockSummary,
    StockSummaryResponse, StockOhlcvBar, StockOhlcvResponse,
    ThemeSummary, ThemeSummaryResponse, Trade,
    TradeSummary, TradesResponse,
    PeriodThemesResponse, PeriodThemeItem,
    PeriodThemeTrendResponse, ThemeTrendSeries,
    PeriodDominantDaysResponse, PeriodDominantDay,
    PeriodDominantTopStocksResponse, PeriodDominantStock,
    PeriodLeadersResponse, PeriodLeaderItem,
    PeriodBreadthResponse, BucketCounts,
    PeriodEventsResponse, PeriodEventItem,
    SuperTrendTradesResponse, SuperTrendTrade, SuperTrendSummary,
    RsBreakoutTradesResponse, RsBreakoutTrade, RsBreakoutSummary,
    PullbackTradesResponse, PullbackTrade, PullbackSummary,
    PeriodThemeRankDay, PeriodThemeRankDaysResponse,
)
from ..analysis.period_analysis import (
    api_period_themes, api_period_theme_trend, api_period_dominant_days,
    api_period_dominant_top_stocks, api_period_theme_rank_days,
    api_period_leaders, api_period_breadth, api_period_leader_events,
)
from ..analysis.perf_timer import reset_db_timer, get_db_time
from ..analysis.supertrend_strategy import api_supertrend_trades, api_stock_ohlcv
from ..analysis.rs_breakout_strategy import api_rs_breakout_trades, api_rs_matrix
from ..analysis.pullback_strategy import api_pullback_trades

# ── 구조화 로그 설정 ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
)
log = logging.getLogger("ssen.api")

# ── App 초기화 ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="SSen Dashboard API",
    description="주도주 분석 대시보드 API (E4+E6)",
    version=API_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(LoggingMetricsMiddleware)   # E6: 요청 로그 + 레이턴시 기록
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["X-Duration-Ms"],
)


_DASH_DIR = _Path(__file__).resolve().parents[3] / "apps" / "dashboard"

@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(_DASH_DIR / "index.html")

@app.on_event("startup")
def startup():
    db.init_pool(minconn=2, maxconn=10)
    _cache_mod.init_redis()          # Redis 연결 시도 (실패 시 TTLCache fallback)
    init_scheduler()                 # 배치 스케줄러 시작

    # cache_warmup은 매일 09:00 cron으로도 등록되지만, 서버가 09:00에 꺼져있으면
    # (재부팅/PC 꺼짐 등) 그날은 영영 실행되지 않아 supertrend/rs-breakout/pullback
    # 첫 조회가 MISS로 10~46초씩 걸리는 문제가 있었음(2026-06-18 실측) — 서버
    # 시작 시점에도 백그라운드로 1회 실행해 cron을 놓쳐도 즉시 보완되게 함
    trigger_cache_warmup_now()

    # RS 전체 행렬 사전 계산 (백그라운드) — 첫 사용자 요청 지연 방지
    def _prewarm_rs_matrix():
        try:
            dv = _get_dataset_version()
            _compute_rs_full(dv)
            log.info(f"RS 전체 행렬 사전 계산 완료 (dataset_version={dv})")
        except Exception as e:
            log.warning(f"RS 전체 행렬 사전 계산 실패: {e}")

    threading.Thread(target=_prewarm_rs_matrix, daemon=True).start()


@app.on_event("shutdown")
def shutdown():
    shutdown_scheduler()
    db.close_pool()


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_dataset_version() -> str:
    info = db.get_dataset_info()
    return info.get("dataset_version") or "unknown"


def _meta(dv: str) -> Meta:
    return Meta(api_version=API_VERSION, dataset_version=dv, generated_at=_now())


class _PeriodPerf:
    """/api/period/* 단계별(캐시/DB쿼리/계산/직렬화) 실행 시간(ms) 로깅 + X-Duration-Ms 응답 헤더.

    사용:
        with _PeriodPerf("period_themes", response) as perf:
            cached = _cache_mod.cache_get(...)
            perf.cache(cached is not None)   # cache HIT/MISS 기록
            if cached:
                return cached
            raw = api_period_themes(...)
            perf.mark("query_calc")   # DB쿼리(db) + 계산(calc)으로 자동 분리
            result = PeriodThemesResponse(...)
            perf.mark("serialize")
            _cache_mod.cache_set(...)
            perf.mark("cache_set")
    """

    def __init__(self, name: str, response: Response):
        self.name = name
        self.response = response
        self.marks: dict[str, float] = {}
        self.cache_hit: Optional[bool] = None
        self._t0 = time.perf_counter()
        self._last = self._t0

    def mark(self, label: str) -> None:
        now = time.perf_counter()
        self.marks[label] = (now - self._last) * 1000
        self._last = now

    def cache(self, hit: bool) -> None:
        self.cache_hit = hit
        self.mark("cache_lookup")

    def __enter__(self) -> "_PeriodPerf":
        reset_db_timer()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            return False
        total_ms = (time.perf_counter() - self._t0) * 1000
        parts = []
        if self.cache_hit is not None:
            parts.append(f"cache={'HIT' if self.cache_hit else 'MISS'}")
        for label, ms in self.marks.items():
            if label == "query_calc":
                db_ms = get_db_time()
                calc_ms = max(ms - db_ms, 0.0)
                parts.append(f"db={db_ms:.1f}ms")
                parts.append(f"calc={calc_ms:.1f}ms")
            else:
                parts.append(f"{label}={ms:.1f}ms")
        parts.append(f"total={total_ms:.1f}ms")
        log.info("[perf] %s %s", self.name, " ".join(parts))
        self.response.headers["X-Duration-Ms"] = f"{total_ms:.1f}"
        return False


# 전체 기간 RS 행렬 — dataset_version이 바뀔 때까지 프로세스 내에 유지 (TTL 캐시와 별도)
_RS_FULL_CACHE: dict[str, Any] = {}
_RS_FULL_LOCK = threading.Lock()


def _compute_rs_full(dv: str):
    with _RS_FULL_LOCK:
        cached = _RS_FULL_CACHE.get(dv)
        if cached is not None:
            return cached
        info = db.get_dataset_info()
        full = api_rs_matrix(info["min_date"], info["max_date"])
        _RS_FULL_CACHE.clear()
        _RS_FULL_CACHE[dv] = full
        return full


def _get_rs_matrix(start: date, end: date, dv: str):
    """기간별 RS 행렬. 전체 기간 행렬을 캐시해두고 슬라이싱 — 연도 전환 시 재계산 회피."""
    cached = _cache_mod.cache_get("rs_matrix", dv, start=str(start), end=str(end))
    if cached is not None:
        return cached

    full = _RS_FULL_CACHE.get(dv)
    if full is None:
        full = _compute_rs_full(dv)

    rs = full.loc[(full.index >= start) & (full.index <= end)]
    _cache_mod.cache_set("rs_matrix", dv, rs, start=str(start), end=str(end))
    return rs


def _rs_lookup(rs_matrix, d: Optional[str], code: Optional[str]) -> Optional[float]:
    if rs_matrix is None or rs_matrix.empty or d is None or code is None:
        return None
    try:
        v = rs_matrix.loc[date.fromisoformat(d), code]
    except (KeyError, ValueError):
        return None
    return round(float(v), 1) if v == v else None  # NaN != NaN


DEFAULT_RULE = "v1.0"


# ── /health ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"], summary="서비스 헬스체크")
def health():
    dv = _get_dataset_version()
    return {"status": "ok", "api_version": API_VERSION, "dataset_version": dv,
            "cache_backend": _cache_mod.cache_stats()["backend"]}


# ── /meta/metrics ─────────────────────────────────────────────────────────────

@app.get("/meta/metrics", tags=["meta"], summary="요청 레이턴시 메트릭 (E6)")
def meta_metrics():
    """엔드포인트별 p50/p95/p99 응답시간 + 요청수 (서버 시작 이후 누적)."""
    return {
        "metrics": get_metrics(),
        "cache": _cache_mod.cache_stats(),
        "generated_at": _now().isoformat(),
    }


# ── /meta/cache/invalidate ────────────────────────────────────────────────────

@app.post("/meta/cache/invalidate", tags=["meta"], summary="캐시 무효화 (E6)")
def cache_invalidate(prefix: str = Query(default="", description="무효화할 캐시 prefix (빈값=전체)")):
    """ETL 완료 후 캐시 수동 무효화. update 파이프라인이 자동 호출."""
    if prefix:
        deleted = _cache_mod.invalidate_prefix(prefix)
    else:
        _cache_mod.cache_clear()
        deleted = -1
    return {"status": "ok", "prefix": prefix or "(all)", "deleted": deleted}


# ── /meta/jobs/trigger ────────────────────────────────────────────────────────

@app.post("/meta/jobs/trigger", tags=["meta"], summary="배치 즉시 실행 (E6)")
def trigger_job(job_id: str = Query(default="e3_refresh")):
    """파생 테이블 재계산을 백그라운드에서 즉시 실행."""
    if job_id == "e3_refresh":
        msg = trigger_e3_now()
        return {"status": "started", "job": job_id, "message": msg}
    if job_id == "cache_warmup":
        msg = trigger_cache_warmup_now()
        return {"status": "started", "job": job_id, "message": msg}
    if job_id == "daily_collect":
        msg = trigger_daily_collect_now()
        return {"status": "started", "job": job_id, "message": msg}
    raise HTTPException(status_code=400, detail=f"Unknown job: {job_id}")


# ── /meta/dataset ────────────────────────────────────────────────────────────

@app.get("/meta/dataset", response_model=DatasetResponse, tags=["meta"],
         summary="데이터셋 버전 / 최종 업데이트 정보")
def meta_dataset():
    """
    대시보드 상단에 표시할 데이터셋 최신성 정보.
    - `dataset_version`: max_date 기반 버전 식별자
    - `last_updated_at`: ETL 완료 시각
    """
    dv = _get_dataset_version()

    cached = _cache_mod.cache_get("meta_dataset", dv)
    if cached:
        return cached

    row = db.get_dataset_info()
    info = DatasetInfo(
        dataset_version=row.get("dataset_version") or dv,
        last_updated_at=str(row.get("last_updated_at") or ""),
        min_date=row.get("min_date"),
        max_date=row.get("max_date"),
        source_file=row.get("source_file"),
    )
    result = DatasetResponse(meta=_meta(dv), data=info)
    _cache_mod.cache_set("meta_dataset", dv, result)
    return result


# ── /leaders/daily ───────────────────────────────────────────────────────────

@app.get("/leaders/daily", response_model=DailyLeaderResponse, tags=["leaders"],
         summary="특정 날짜의 주도 테마 + 주도주")
def leaders_daily(
    query_date: date = Query(..., alias="date", description="조회 날짜 (YYYY-MM-DD)"),
    rule_version: str = Query(default=DEFAULT_RULE),
):
    """
    해당 날짜에 `is_top_theme=TRUE`인 테마(주도 테마)와 주도주를 반환.
    `signal_date`로 사용할 수 있으며 체결은 다음 거래일에 수행해야 함.
    """
    dv = _get_dataset_version()
    cached = _cache_mod.cache_get("leaders_daily", dv, date=str(query_date), rv=rule_version)
    if cached:
        return cached

    rows = db.get_daily_leaders(query_date, rule_version, dv)
    result = DailyLeaderResponse(
        meta=_meta(dv),
        data=[DailyLeader(**r) for r in rows],
    )
    _cache_mod.cache_set("leaders_daily", dv, result, date=str(query_date), rv=rule_version)
    return result


# ── /leaders/regimes ─────────────────────────────────────────────────────────

@app.get("/leaders/regimes", response_model=RegimesResponse, tags=["leaders"],
         summary="기간 내 주도 테마 레짐 목록")
def leaders_regimes(
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
    rule_version: str = Query(default=DEFAULT_RULE),
    limit: int  = Query(default=200, ge=1, le=1000),
):
    """주도 테마가 연속 유지된 구간(레짐) 목록. 갈아타기 시점 파악에 사용."""
    dv = _get_dataset_version()
    cached = _cache_mod.cache_get("regimes", dv,
                                   start=str(start), end=str(end),
                                   rv=rule_version, limit=limit)
    if cached:
        return cached

    rows = db.get_regimes(start, end, rule_version, dv, limit)
    result = RegimesResponse(
        meta=_meta(dv),
        total=len(rows),
        data=[Regime(**r) for r in rows],
    )
    _cache_mod.cache_set("regimes", dv, result,
                          start=str(start), end=str(end),
                          rv=rule_version, limit=limit)
    return result


# ── /trades ──────────────────────────────────────────────────────────────────

@app.get("/trades", response_model=TradesResponse, tags=["trades"],
         summary="기간 내 트레이드 로그 + 성과 요약")
def trades(
    start: date = Query(..., description="시작일"),
    end: date   = Query(..., description="종료일"),
    rule_version: str = Query(default=DEFAULT_RULE),
    code: Optional[str]  = Query(default=None, description="종목코드 필터"),
    theme: Optional[str] = Query(default=None, description="테마 필터"),
    limit: int = Query(default=500, ge=1, le=2000),
):
    """
    진입/청산 트레이드 로그.
    - `signal_date` = 신호 발생일(t) — 이 날의 데이터로 신호 결정
    - `entry_date`  = 체결일(t+1) — 이 날 close_price로 체결
    """
    dv = _get_dataset_version()
    cache_key = dict(start=str(start), end=str(end), rv=rule_version,
                     code=code, theme=theme, limit=limit)
    cached = _cache_mod.cache_get("trades", dv, **cache_key)
    if cached:
        return cached

    rows = db.get_trades(start, end, rule_version, dv, code, theme, limit)
    summary_dict = db.compute_trade_summary(rows)

    result = TradesResponse(
        meta=_meta(dv),
        total=len(rows),
        summary=TradeSummary(**summary_dict),
        data=[Trade(**r) for r in rows],
    )
    _cache_mod.cache_set("trades", dv, result, **cache_key)
    return result


# ── /stocks/{code}/summary ────────────────────────────────────────────────────

@app.get("/stocks/{code}/summary", response_model=StockSummaryResponse,
         tags=["stocks"], summary="특정 종목 기간 요약")
def stock_summary(
    code: str,
    start: date = Query(...),
    end: date   = Query(...),
    rule_version: str = Query(default=DEFAULT_RULE),
):
    """종목의 출현 빈도, 평균 순위, 등락률, 트레이드 결과 요약."""
    dv = _get_dataset_version()
    cached = _cache_mod.cache_get("stock_summary", dv,
                                   code=code, start=str(start),
                                   end=str(end), rv=rule_version)
    if cached:
        return cached

    data = db.get_stock_summary(code, start, end, rule_version, dv)
    if not data:
        raise HTTPException(status_code=404, detail=f"종목 {code} 데이터 없음")

    result = StockSummaryResponse(meta=_meta(dv), data=StockSummary(**data))
    _cache_mod.cache_set("stock_summary", dv, result,
                          code=code, start=str(start),
                          end=str(end), rv=rule_version)
    return result


@app.get("/api/stocks/{code}/ohlcv", response_model=StockOhlcvResponse,
         tags=["stocks"], summary="종목 캔들 데이터 (end일 기준 최근 N봉)")
def stock_ohlcv(
    code: str,
    end: date = Query(...),
    bars: int = Query(default=100, ge=1, le=500),
):
    """차트 표시용 — end일(포함) 기준 최근 bars개 봉의 OHLCV."""
    dv = _get_dataset_version()
    cached = _cache_mod.cache_get("stock_ohlcv", dv, code=code, end=str(end), bars=bars)
    if cached is not None:
        return cached

    df = api_stock_ohlcv(code, end, bars)
    if df.empty:
        raise HTTPException(status_code=404, detail=f"종목 {code} 데이터 없음")

    result = StockOhlcvResponse(
        meta=_meta(dv),
        code=code,
        name=str(df["name"].iloc[-1]),
        data=[StockOhlcvBar(**r) for r in df.drop(columns=["name"]).astype(
            {"date": "string"}).to_dict("records")],
    )
    _cache_mod.cache_set("stock_ohlcv", dv, result, code=code, end=str(end), bars=bars)
    return result


# ── /themes/{theme}/summary ───────────────────────────────────────────────────

@app.get("/themes/{theme}/summary", response_model=ThemeSummaryResponse,
         tags=["themes"], summary="특정 테마 기간 요약")
def theme_summary(
    theme: str,
    start: date = Query(...),
    end: date   = Query(...),
    rule_version: str = Query(default=DEFAULT_RULE),
):
    """테마의 레짐 횟수, 평균 지속일, 트레이드 승률/수익률 요약."""
    dv = _get_dataset_version()
    cached = _cache_mod.cache_get("theme_summary", dv,
                                   theme=theme, start=str(start),
                                   end=str(end), rv=rule_version)
    if cached:
        return cached

    data = db.get_theme_summary(theme, start, end, rule_version, dv)
    if not data:
        raise HTTPException(status_code=404, detail=f"테마 '{theme}' 데이터 없음")

    result = ThemeSummaryResponse(meta=_meta(dv), data=ThemeSummary(**data))
    _cache_mod.cache_set("theme_summary", dv, result,
                          theme=theme, start=str(start),
                          end=str(end), rv=rule_version)
    return result


# ── /api/period/* (E8) ───────────────────────────────────────────────────────

def _period_cache_key(endpoint: str, start: date, end: date, dv: str) -> tuple:
    return (endpoint, str(start), str(end), dv)


@app.get("/api/period/themes", response_model=PeriodThemesResponse,
         tags=["period"], summary="§1 테마 분석 (복리·dedup·종합점수·로테이션)")
def period_themes(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
):
    with _PeriodPerf("period_themes", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_themes", dv, start=str(start), end=str(end))
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_period_themes(start, end)
        perf.mark("query_calc")
        result = PeriodThemesResponse(
            meta=_meta(dv), start=start, end=end,
            rising  =[PeriodThemeItem(**r) for r in raw["rising"]],
            falling =[PeriodThemeItem(**r) for r in raw["falling"]],
            rotating=[PeriodThemeItem(**r) for r in raw["rotating"]],
            all     =[PeriodThemeItem(**r) for r in raw["all"]],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_themes", dv, result, start=str(start), end=str(end))
        perf.mark("cache_set")
    return result


@app.get("/api/period/theme-trend", response_model=PeriodThemeTrendResponse,
         tags=["period"], summary="§1-T 테마 추이 (상위 테마 누적수익률 시계열)")
def period_theme_trend(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
    top: int    = Query(default=20, ge=1, le=50, description="표시할 상위 테마 수"),
):
    with _PeriodPerf("period_theme_trend", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_theme_trend", dv, start=str(start), end=str(end), top=top)
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_period_theme_trend(start, end, top)
        perf.mark("query_calc")
        result = PeriodThemeTrendResponse(
            meta=_meta(dv), start=start, end=end,
            dates =raw["dates"],
            series=[ThemeTrendSeries(**s) for s in raw["series"]],
            kospi =raw["kospi"],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_theme_trend", dv, result, start=str(start), end=str(end), top=top)
        perf.mark("cache_set")
    return result


@app.get("/api/period/dominant-days", response_model=PeriodDominantDaysResponse,
         tags=["period"], summary="§1-D 압도적 주도 테마일 (등락률·종목수·거래대금 모두 1위)")
def period_dominant_days(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
):
    with _PeriodPerf("period_dominant_days", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_dominant_days", dv, start=str(start), end=str(end))
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_period_dominant_days(start, end)
        perf.mark("query_calc")
        result = PeriodDominantDaysResponse(
            meta=_meta(dv), start=start, end=end,
            days =[PeriodDominantDay(**r) for r in raw["days"]],
            total=raw["total"],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_dominant_days", dv, result, start=str(start), end=str(end))
        perf.mark("cache_set")
    return result


@app.get("/api/period/theme-rank-days", response_model=PeriodThemeRankDaysResponse,
         tags=["period"], summary="일별 주도 테마(거래대금 1위) 2위 대비 지표")
def period_theme_rank_days(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
):
    with _PeriodPerf("period_theme_rank_days", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_theme_rank_days", dv, start=str(start), end=str(end))
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_period_theme_rank_days(start, end)
        perf.mark("query_calc")
        result = PeriodThemeRankDaysResponse(
            meta=_meta(dv), start=start, end=end,
            days =[PeriodThemeRankDay(**r) for r in raw["days"]],
            total=raw["total"],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_theme_rank_days", dv, result, start=str(start), end=str(end))
        perf.mark("cache_set")
    return result


@app.get("/api/period/dominant-top-stocks", response_model=PeriodDominantTopStocksResponse,
         tags=["period"], summary="§3 압도적 주도 테마일의 1~2위 종목 상세")
def period_dominant_top_stocks(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
    top_rank: int = Query(2, ge=1, le=10, description="테마 내 순위 상한 (theme1_rank <= top_rank)"),
):
    with _PeriodPerf("period_dominant_top_stocks", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_dominant_top_stocks", dv, start=str(start), end=str(end), top_rank=top_rank)
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_period_dominant_top_stocks(start, end, top_rank)
        rs_matrix = _get_rs_matrix(start, end, dv)
        perf.mark("query_calc")
        result = PeriodDominantTopStocksResponse(
            meta=_meta(dv), start=start, end=end,
            stocks=[PeriodDominantStock(**s, rs=_rs_lookup(rs_matrix, s["date"], s["code"])) for s in raw["stocks"]],
            total=raw["total"],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_dominant_top_stocks", dv, result, start=str(start), end=str(end), top_rank=top_rank)
        perf.mark("cache_set")
    return result


@app.get("/api/period/supertrend-trades", response_model=SuperTrendTradesResponse,
         tags=["period"], summary="§T 슈퍼트렌드(21,3) 주도테마 돌파 전략 백테스트")
def period_supertrend_trades(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
):
    with _PeriodPerf("period_supertrend_trades", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_supertrend_trades", dv, start=str(start), end=str(end))
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_supertrend_trades(start, end)
        perf.mark("query_calc")
        result = SuperTrendTradesResponse(
            meta=_meta(dv), start=start, end=end,
            summary=SuperTrendSummary(**raw["summary"]),
            trades=[SuperTrendTrade(**t) for t in raw["trades"]],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_supertrend_trades", dv, result, start=str(start), end=str(end))
        perf.mark("cache_set")
    return result


@app.get("/api/period/rs-breakout-trades", response_model=RsBreakoutTradesResponse,
         tags=["period"], summary="§R RS≥85 + 거래대금 1천억 + 슈퍼트렌드 돌파 전략 백테스트")
def period_rs_breakout_trades(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
):
    with _PeriodPerf("period_rs_breakout_trades", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_rs_breakout_trades", dv, start=str(start), end=str(end))
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_rs_breakout_trades(start, end)
        perf.mark("query_calc")
        result = RsBreakoutTradesResponse(
            meta=_meta(dv), start=start, end=end,
            summary=RsBreakoutSummary(**raw["summary"]),
            trades=[RsBreakoutTrade(**t) for t in raw["trades"]],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_rs_breakout_trades", dv, result, start=str(start), end=str(end))
        perf.mark("cache_set")
    return result


@app.get("/api/period/pullback-trades", response_model=PullbackTradesResponse,
         tags=["period"], summary="§P 기준봉 눌림목 타점 전략 백테스트")
def period_pullback_trades(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
):
    with _PeriodPerf("period_pullback_trades", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_pullback_trades", dv, start=str(start), end=str(end))
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_pullback_trades(start, end)
        perf.mark("query_calc")
        result = PullbackTradesResponse(
            meta=_meta(dv), start=start, end=end,
            summary=PullbackSummary(**raw["summary"]),
            trades=[PullbackTrade(**t) for t in raw["trades"]],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_pullback_trades", dv, result, start=str(start), end=str(end))
        perf.mark("cache_set")
    return result


@app.get("/api/period/leaders", response_model=PeriodLeadersResponse,
         tags=["period"], summary="§2 강세 주도종목 (복합점수·커버리지·첫주도신호일)")
def period_leaders(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
):
    with _PeriodPerf("period_leaders", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_leaders", dv, start=str(start), end=str(end))
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_period_leaders(start, end)
        rs_matrix = _get_rs_matrix(start, end, dv)
        perf.mark("query_calc")
        result = PeriodLeadersResponse(
            meta=_meta(dv), start=start, end=end,
            total  =raw["total"],
            leaders=[PeriodLeaderItem(**r, first_signal_rs=_rs_lookup(rs_matrix, r["first_signal_date"], r["code"]))
                     for r in raw["leaders"]],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_leaders", dv, result, start=str(start), end=str(end))
        perf.mark("cache_set")
    return result


@app.get("/api/period/breadth", response_model=PeriodBreadthResponse,
         tags=["period"], summary="§3 강세 분포 (기여점수 버킷 × 규모·시장)")
def period_breadth(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
):
    with _PeriodPerf("period_breadth", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_breadth", dv, start=str(start), end=str(end))
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_period_breadth(start, end)
        perf.mark("query_calc")
        result = PeriodBreadthResponse(
            meta=_meta(dv), start=start, end=end,
            by_size  ={k: BucketCounts(**v) for k, v in raw["by_size"].items()},
            by_market={k: BucketCounts(**v) for k, v in raw["by_market"].items()},
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_breadth", dv, result, start=str(start), end=str(end))
        perf.mark("cache_set")
    return result


@app.get("/api/period/leader-events", response_model=PeriodEventsResponse,
         tags=["period"], summary="§4 강세주도 이벤트 (기여점수≥7 타임라인)")
def period_leader_events(
    response: Response,
    start: date = Query(..., description="시작일 (YYYY-MM-DD)"),
    end: date   = Query(..., description="종료일 (YYYY-MM-DD)"),
):
    with _PeriodPerf("period_leader_events", response) as perf:
        dv = _get_dataset_version()
        cached = _cache_mod.cache_get("period_events", dv, start=str(start), end=str(end))
        perf.cache(cached is not None)
        if cached:
            return cached

        raw = api_period_leader_events(start, end)
        rs_matrix = _get_rs_matrix(start, end, dv)
        perf.mark("query_calc")
        result = PeriodEventsResponse(
            meta=_meta(dv), start=start, end=end,
            total =raw["total"],
            events=[PeriodEventItem(**r, rs=_rs_lookup(rs_matrix, r["date"], r["code"])) for r in raw["events"]],
        )
        perf.mark("serialize")
        _cache_mod.cache_set("period_events", dv, result, start=str(start), end=str(end))
        perf.mark("cache_set")
    return result


# ── /perf ─────────────────────────────────────────────────────────────────────

@app.get("/perf", response_model=PerfResponse, tags=["meta"],
         summary="대표 엔드포인트 p95 응답시간 측정")
def perf(n: int = Query(default=20, ge=5, le=100)):
    """n회 반복 측정하여 p50/p95/p99/max 응답시간(ms)을 반환."""
    dv = _get_dataset_version()

    def measure(fn) -> list[float]:
        times = []
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            times.append((time.perf_counter() - t0) * 1000)
        return times

    tests = [
        ("GET /leaders/regimes (2025년)",
         lambda: db.get_regimes(date(2025, 1, 1), date(2025, 12, 31), DEFAULT_RULE, dv)),
        ("GET /trades (2025년)",
         lambda: db.get_trades(date(2025, 1, 1), date(2025, 12, 31), DEFAULT_RULE, dv)),
        ("GET /meta/dataset",
         lambda: db.get_dataset_info()),
    ]

    results = []
    for label, fn in tests:
        times = measure(fn)
        arr = np.array(times)
        results.append(PerfResult(
            endpoint=label,
            n_samples=n,
            p50_ms=round(float(np.percentile(arr, 50)), 2),
            p95_ms=round(float(np.percentile(arr, 95)), 2),
            p99_ms=round(float(np.percentile(arr, 99)), 2),
            max_ms=round(float(arr.max()), 2),
        ))

    return PerfResponse(meta=_meta(dv), results=results)

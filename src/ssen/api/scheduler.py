"""
E6: 배치 스케줄러 (APScheduler).

등록된 작업:
  - daily_collect: 매일 16:30 거래대금/코스피시세/ADR/OHLCV 수집 (daily_update.py)
  - e3_refresh: 매일 02:00 파생 테이블 재계산
  - cache_evict: 1시간마다 만료 캐시 정리 (TTLCache 자동, Redis TTL 자동)

cache_warmup(1M/3M/6M/1Y/YTD 트레이드 캐시 예열)은 cron이 아니라 main.py의
FastAPI startup 이벤트에서 단일 트리거로 실행됨 — 서버 켜질 때마다 1회
(trigger_cache_warmup_now() 직접 호출, 여기 스케줄러엔 등록 안 함).
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger("ssen.scheduler")

ROOT = Path(__file__).resolve().parents[3]
_scheduler: BackgroundScheduler | None = None


def _run_e3_refresh() -> None:
    """파생 테이블 전체 재계산 (daily batch)."""
    log.info("배치 시작: e3_refresh")
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from ssen.strategy.backtest import run as backtest_run
        from ssen.strategy.rules import DEFAULT_PARAMS
        from ssen.api import cache as _cache

        result = backtest_run(
            parquet_dir=ROOT / "data" / "parquet",
            params=DEFAULT_PARAMS,
        )
        # 재계산 완료 → 캐시 무효화
        _cache.invalidate_prefix("leaders")
        _cache.invalidate_prefix("trades")
        log.info("배치 완료: e3_refresh, trades=%s", result.get("n_trades"))
    except Exception as e:
        log.error("배치 실패: e3_refresh: %s", e)


def _run_daily_collect() -> None:
    """일일 데이터 수집 (거래대금/코스피시세/ADR/OHLCV) — ssen.update.daily_update.run().

    2026-06-17 발견된 문제: OHLCV 수집(collect_ohlcv.py)이 daily_update.py/scheduler
    어디에도 연결되지 않은 완전 독립 수동 스크립트였던 탓에, 누군가 수동 실행을 깜빡해
    OHLCV가 6일간 멈춰 RS 계산이 전부 깨진 사고가 있었음 — 재발 방지를 위해 반드시
    스케줄러에 등록. 장마감(15:30) + 키움 15:40~16:00 수집가능 시각을 감안해 16:30 실행.

    2026-06-18 추가: 이 배치(E3 포함)가 끝나면 dataset_version이 바뀌는데, 캐시 키에
    dataset_version이 포함돼 있어 startup 때 미리 데워둔 캐시가 통째로 무효화됨 —
    서버를 안 내려도 매일 16:30 직후 supertrend/rs-breakout/pullback이 다시 MISS로
    느려지는 문제가 있었음. 완료 직후 cache_warmup을 체이닝해 새 dataset_version
    기준으로 즉시 재워밍업.
    """
    log.info("배치 시작: daily_collect")
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from ssen.update.daily_update import run as daily_update_run

        result = daily_update_run()
        log.info("배치 완료: daily_collect, %s", result.get("status"))
    except Exception as e:
        log.error("배치 실패: daily_collect: %s", e)
        return

    # dataset_version 변경 후 캐시 재워밍업 (실패해도 daily_collect 자체는 이미 완료된 상태)
    _run_cache_warmup()


def _run_cache_warmup() -> None:
    """캐시 워밍업: supertrend/rs-breakout/pullback trades를 자주 쓰는
    기간(1M/3M/6M/1Y/YTD)에 대해 미리 계산하여 캐시에 적재.

    Assumptions:
      - daily_update.py(E3 배치)는 코드상 존재하지 않으므로, 이 프로젝트의
        E3 배치는 scheduler.py의 _run_e3_refresh(매일 02:00)로 간주.
        02:00 배치가 끝나 dataset_version이 안정된 이후 시점인 매일
        09:00에 별도 작업으로 등록.
      - 기준일(end)은 date.today() — 대시보드 기본 종료일과 동일.
    """
    log.info("배치 시작: cache_warmup")
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from dateutil.relativedelta import relativedelta

        from ssen.api import cache as _cache
        from ssen.api import db
        from ssen.api.schemas import (
            API_VERSION, Meta,
            SuperTrendTradesResponse, SuperTrendSummary, SuperTrendTrade,
            RsBreakoutTradesResponse, RsBreakoutSummary, RsBreakoutTrade,
            PullbackTradesResponse, PullbackSummary, PullbackTrade,
        )
        from ssen.analysis.supertrend_strategy import api_supertrend_trades
        from ssen.analysis.rs_breakout_strategy import api_rs_breakout_trades
        from ssen.analysis.pullback_strategy import api_pullback_trades

        dv = db.get_dataset_info().get("dataset_version") or "unknown"
        meta = Meta(api_version=API_VERSION, dataset_version=dv,
                     generated_at=datetime.now(timezone.utc))

        end = date.today()
        periods = {
            "1M": end - relativedelta(months=1),
            "3M": end - relativedelta(months=3),
            "6M": end - relativedelta(months=6),
            "1Y": end - relativedelta(years=1),
            "YTD": date(end.year, 1, 1),
        }

        targets = [
            ("period_supertrend_trades", api_supertrend_trades,
             SuperTrendTradesResponse, SuperTrendSummary, SuperTrendTrade),
            ("period_rs_breakout_trades", api_rs_breakout_trades,
             RsBreakoutTradesResponse, RsBreakoutSummary, RsBreakoutTrade),
            ("period_pullback_trades", api_pullback_trades,
             PullbackTradesResponse, PullbackSummary, PullbackTrade),
        ]

        n_done = 0
        for label, start in periods.items():
            for namespace, fn, response_cls, summary_cls, trade_cls in targets:
                try:
                    raw = fn(start, end)
                    result = response_cls(
                        meta=meta, start=start, end=end,
                        summary=summary_cls(**raw["summary"]),
                        trades=[trade_cls(**t) for t in raw["trades"]],
                    )
                    _cache.cache_set(namespace, dv, result, start=str(start), end=str(end))
                    n_done += 1
                except Exception as e:
                    log.error("캐시 워밍업 실패: %s %s (%s~%s): %s", namespace, label, start, end, e)
        log.info("배치 완료: cache_warmup, %d개 캐시 적재", n_done)
    except Exception as e:
        log.error("배치 실패: cache_warmup: %s", e)


def _log_cache_stats() -> None:
    from ssen.api import cache as _cache
    stats = _cache.cache_stats()
    log.info("캐시 통계: %s", stats)


def init_scheduler() -> BackgroundScheduler:
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # 매일 16:30 데이터 수집 (거래대금/코스피시세/ADR/OHLCV — daily_update.py)
    _scheduler.add_job(
        _run_daily_collect,
        CronTrigger(hour=16, minute=30),
        id="daily_collect",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # 매일 02:00 파생 테이블 재계산
    _scheduler.add_job(
        _run_e3_refresh,
        CronTrigger(hour=2, minute=0),
        id="e3_refresh",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # cache_warmup은 더 이상 cron으로 등록하지 않음 — main.py FastAPI startup
    # 이벤트에서 trigger_cache_warmup_now()로 단일 트리거 통일(2026-06-18).
    # 09:00 cron은 서버가 그 시각에 꺼져있으면 그날 영영 실행되지 않는 문제가
    # 있었고(실제 발생), 이 프로젝트는 사용자가 매일 직접 서버를 켜는 운영 방식이라
    # startup 트리거 하나로 충분함. cron도 같이 두면 서버 재시작 시점이 09:00 근처일
    # 때 중복 실행될 수 있어 제거.

    # 1시간마다 캐시 통계 로그
    _scheduler.add_job(
        _log_cache_stats,
        IntervalTrigger(hours=1),
        id="cache_stats",
        replace_existing=True,
    )

    _scheduler.start()
    log.info("스케줄러 시작: %d개 작업 등록", len(_scheduler.get_jobs()))
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("스케줄러 종료")


def trigger_e3_now() -> str:
    """즉시 e3_refresh 실행 (수동 트리거)."""
    import threading
    t = threading.Thread(target=_run_e3_refresh, daemon=True)
    t.start()
    return "e3_refresh 백그라운드 시작"


def trigger_daily_collect_now() -> str:
    """즉시 daily_collect 실행 (수동 트리거)."""
    import threading
    t = threading.Thread(target=_run_daily_collect, daemon=True)
    t.start()
    return "daily_collect 백그라운드 시작"


def trigger_cache_warmup_now() -> str:
    """즉시 cache_warmup 실행 (수동 트리거)."""
    import threading
    t = threading.Thread(target=_run_cache_warmup, daemon=True)
    t.start()
    return "cache_warmup 백그라운드 시작"

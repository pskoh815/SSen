"""DB 쿼리 함수 (E4). 모든 파라미터 바인딩 — SQL 인젝션 방지."""
from __future__ import annotations
import json
import os
from contextlib import contextmanager
from datetime import date
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

_DB_URL = os.environ.get("SSEN_DB_URL", "postgresql://ssen:ssen@127.0.0.1:5432/ssen")
_pool: Optional[ThreadedConnectionPool] = None


def init_pool(minconn: int = 2, maxconn: int = 10) -> None:
    global _pool
    _pool = ThreadedConnectionPool(minconn, maxconn, _DB_URL)


def close_pool() -> None:
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None


@contextmanager
def get_conn():
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


def _fetchall(sql: str, params: tuple = ()) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _fetchone(sql: str, params: tuple = ()) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


# ── meta ────────────────────────────────────────────────────────────────────

def get_dataset_info() -> dict:
    # Parquet manifest 대신 etl_runs에서 최신 정보 조회
    row = _fetchone("""
        SELECT dataset_version,
               finished_at   AS last_updated_at,
               min_date,
               max_date,
               input_files[1] AS source_file
        FROM   etl_runs
        WHERE  status = 'done'
        ORDER  BY finished_at DESC
        LIMIT  1
    """)
    return row or {}


# ── /leaders/daily ───────────────────────────────────────────────────────────

def get_daily_leaders(
    query_date: date,
    rule_version: str,
    dataset_version: str,
) -> list[dict]:
    # dataset_version 정확히 일치(=)로 거르면, E3가 좁은 lookback 윈도우로 증분
    # 재계산될 때마다 새 dataset_version이 그 좁은 구간에만 찍혀 그 밖의(예: 수년치)
    # 기존 데이터가 영영 안 보이게 됨(2026-06-18 실측: derived_trades가 동일하게
    # 깨져있던 걸 발견 — 같은 패턴이라 여기도 함께 수정). (date,theme1) 기준으로
    # 가장 최신 dataset_version의 행만 남기는 dedup으로 교체.
    return _fetchall("""
        SELECT date, theme1, theme_amount, leader_code, leader_name,
               leader_rank, leader_close, avg_change_pct, stock_count
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY date, theme1 ORDER BY dataset_version DESC
            ) AS rn
            FROM   derived_theme_daily
            WHERE  date = %s
              AND  is_top_theme = TRUE
              AND  rule_version = %s
        ) sub
        WHERE  rn = 1
        ORDER  BY theme_amount DESC NULLS LAST
    """, (query_date, rule_version))


# ── /leaders/regimes ─────────────────────────────────────────────────────────

def get_regimes(
    start: date,
    end: date,
    rule_version: str,
    dataset_version: str,
    limit: int = 200,
) -> list[dict]:
    # 겹침(overlap) 방식: 기간과 일부라도 겹치는 레짐을 모두 반환
    # 조건: regime.start_date <= query_end AND regime.end_date >= query_start
    # dataset_version 정확히 일치 필터는 get_daily_leaders와 동일한 이유로 제거
    # (theme1,start_date,end_date) 기준 최신 dataset_version만 남기는 dedup으로 교체.
    return _fetchall("""
        SELECT regime_id, theme1, leader_code, leader_name,
               start_date, end_date, duration_days, avg_theme_amount
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY theme1, start_date, end_date ORDER BY dataset_version DESC
            ) AS rn
            FROM   derived_leader_regime
            WHERE  start_date <= %s
              AND  end_date   >= %s
              AND  rule_version = %s
        ) sub
        WHERE  rn = 1
        ORDER  BY start_date
        LIMIT  %s
    """, (end, start, rule_version, limit))


# ── /trades ──────────────────────────────────────────────────────────────────

def get_trades(
    start: date,
    end: date,
    rule_version: str,
    dataset_version: str,
    code: Optional[str] = None,
    theme: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    # 겹침 방식: 진입일이 end 이전 AND (미청산 OR 청산일이 start 이후)
    # → 해당 기간 동안 활성이었던 포지션 모두 표시
    #
    # 2026-06-18 발견한 회귀: dataset_version 정확히 일치(=) 필터 때문에, daily_update.py가
    # 좁은 lookback(60일)으로 E3를 증분 재계산할 때마다 그 좁은 구간에만 새 dataset_version이
    # 찍혀, 그 밖의 2020~2026 전체 과거 트레이드(760행 중 754행)가 영영 조회 불가능해짐
    # (실측: 2026-06-17 버전엔 단 3건만 존재, 어떤 기간을 조회해도 그 3건만 반환되거나
    # 겹치지 않으면 0건). dataset_version 필터를 제거하고 (code,signal_date,entry_date)
    # 기준 최신 dataset_version 행만 남기는 dedup으로 교체 — 과거 데이터를 가리지 않으면서
    # 같은 트레이드가 재계산되면 최신값으로 자동 교체.
    filters = [
        "entry_date <= %s",
        "(exit_date IS NULL OR exit_date >= %s)",
    ]
    params: list = [end, start]

    if code:
        filters.append("code = %s")
        params.append(code.zfill(6))
    if theme:
        filters.append("theme1 = %s")
        params.append(theme)

    where = " AND ".join(filters)
    final_params = tuple([rule_version] + params + [limit])

    return _fetchall(f"""
        SELECT trade_id, regime_id, code, name, theme1,
               signal_date, entry_date, entry_price,
               exit_date, exit_price, exit_reason,
               pnl_pct, fee_pct, net_pnl_pct, hold_days
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY code, signal_date, entry_date ORDER BY dataset_version DESC
            ) AS rn
            FROM   derived_trades
            WHERE  rule_version = %s
        ) sub
        WHERE  rn = 1 AND {where}
        ORDER  BY entry_date
        LIMIT  %s
    """, final_params)


def compute_trade_summary(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("exit_reason") != "open" and t.get("net_pnl_pct") is not None]
    if not closed:
        return {
            "total_trades": len(trades), "closed_trades": 0,
            "win_rate_pct": None, "avg_net_pnl_pct": None,
            "total_net_pnl_pct": None, "max_drawdown_pct": None,
            "avg_hold_days": None,
        }

    pnls = [t["net_pnl_pct"] for t in closed]
    wins = sum(1 for p in pnls if p > 0)

    # 누적 수익률 & MDD
    cumret = 1.0
    peak = 1.0
    mdd = 0.0
    for p in pnls:
        cumret *= (1 + p / 100)
        peak = max(peak, cumret)
        dd = (cumret - peak) / peak * 100
        mdd = min(mdd, dd)

    hold_days = [t["hold_days"] for t in closed if t.get("hold_days") is not None]

    return {
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "win_rate_pct": round(wins / len(closed) * 100, 1),
        "avg_net_pnl_pct": round(sum(pnls) / len(pnls), 2),
        "total_net_pnl_pct": round((cumret - 1) * 100, 2),
        "max_drawdown_pct": round(mdd, 2),
        "avg_hold_days": round(sum(hold_days) / len(hold_days), 1) if hold_days else None,
    }


# ── /stocks/{code}/summary ────────────────────────────────────────────────────

def get_stock_summary(
    code: str,
    start: date,
    end: date,
    rule_version: str,
    dataset_version: str,
) -> Optional[dict]:
    code = code.zfill(6)

    # fact_daily_stock 기반 출현 통계
    appear = _fetchone("""
        SELECT COUNT(DISTINCT date) AS appear_days,
               AVG(rank)            AS avg_rank,
               AVG(change_pct)      AS avg_change_pct,
               MAX(name)            AS name,
               MODE() WITHIN GROUP (ORDER BY theme1) AS top_theme1
        FROM   fact_daily_stock
        WHERE  code = %s
          AND  date BETWEEN %s AND %s
    """, (code, start, end))

    # derived_trades 기반 트레이드 통계
    # dataset_version exact-match 제거 — get_trades와 동일한 이유(narrow lookback
    # 증분 재계산 시 과거 데이터가 가려지는 회귀), (code,signal_date,entry_date) dedup으로 교체
    trade_stats = _fetchone("""
        SELECT COUNT(*)          AS trade_count,
               MAX(net_pnl_pct)  AS best_pnl_pct,
               MIN(net_pnl_pct)  AS worst_pnl_pct
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY code, signal_date, entry_date ORDER BY dataset_version DESC
            ) AS rn
            FROM   derived_trades
            WHERE  code = %s
              AND  entry_date BETWEEN %s AND %s
              AND  rule_version = %s
        ) sub
        WHERE  rn = 1
    """, (code, start, end, rule_version))

    if not appear:
        return None

    return {
        "code": code,
        "name": appear.get("name"),
        "appear_days": appear.get("appear_days", 0),
        "avg_rank": round(float(appear["avg_rank"]), 1) if appear.get("avg_rank") else None,
        "avg_change_pct": round(float(appear["avg_change_pct"]), 2) if appear.get("avg_change_pct") else None,
        "top_theme1": appear.get("top_theme1"),
        "best_pnl_pct": float(trade_stats["best_pnl_pct"]) if trade_stats and trade_stats.get("best_pnl_pct") else None,
        "worst_pnl_pct": float(trade_stats["worst_pnl_pct"]) if trade_stats and trade_stats.get("worst_pnl_pct") else None,
        "trade_count": int(trade_stats["trade_count"]) if trade_stats else 0,
    }


# ── /themes/{theme}/summary ───────────────────────────────────────────────────

def get_theme_summary(
    theme: str,
    start: date,
    end: date,
    rule_version: str,
    dataset_version: str,
) -> Optional[dict]:
    # dataset_version exact-match 제거 (get_regimes/get_trades와 동일한 이유) —
    # 각각 자연키((theme1,start_date,end_date) / (code,signal_date,entry_date))
    # 기준 최신 dataset_version 행만 남기는 dedup으로 교체
    regime_stats = _fetchone("""
        SELECT COUNT(*)              AS regime_count,
               SUM(duration_days)    AS total_duration_days,
               AVG(duration_days)    AS avg_duration_days,
               MODE() WITHIN GROUP (ORDER BY leader_code) AS top_leader_code,
               MODE() WITHIN GROUP (ORDER BY leader_name) AS top_leader_name
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY theme1, start_date, end_date ORDER BY dataset_version DESC
            ) AS rn
            FROM   derived_leader_regime
            WHERE  theme1 = %s
              AND  start_date >= %s
              AND  end_date   <= %s
              AND  rule_version = %s
        ) sub
        WHERE  rn = 1
    """, (theme, start, end, rule_version))

    trade_stats = _fetchone("""
        SELECT COUNT(*)   AS trade_count,
               AVG(CASE WHEN net_pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100 AS win_rate_pct,
               AVG(net_pnl_pct) AS avg_net_pnl_pct
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY code, signal_date, entry_date ORDER BY dataset_version DESC
            ) AS rn
            FROM   derived_trades
            WHERE  theme1 = %s
              AND  entry_date BETWEEN %s AND %s
              AND  exit_reason != 'open'
              AND  rule_version = %s
        ) sub
        WHERE  rn = 1
    """, (theme, start, end, rule_version))

    if not regime_stats or not regime_stats.get("regime_count"):
        return None

    return {
        "theme1": theme,
        "regime_count": int(regime_stats["regime_count"]),
        "total_duration_days": int(regime_stats["total_duration_days"] or 0),
        "avg_duration_days": round(float(regime_stats["avg_duration_days"]), 1) if regime_stats.get("avg_duration_days") else None,
        "trade_count": int(trade_stats["trade_count"]) if trade_stats else 0,
        "win_rate_pct": round(float(trade_stats["win_rate_pct"]), 1) if trade_stats and trade_stats.get("win_rate_pct") else None,
        "avg_net_pnl_pct": round(float(trade_stats["avg_net_pnl_pct"]), 2) if trade_stats and trade_stats.get("avg_net_pnl_pct") else None,
        "top_leader_code": regime_stats.get("top_leader_code"),
        "top_leader_name": regime_stats.get("top_leader_name"),
    }

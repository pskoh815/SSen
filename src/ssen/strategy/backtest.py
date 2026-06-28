"""
E3: 파생 테이블 계산 + 백테스트 러너.

Usage:
    python -m ssen.strategy.backtest [OPTIONS]

Options:
    --parquet-dir PATH
    --start-date YYYY-MM-DD   (증분: 기본 전체)
    --end-date   YYYY-MM-DD
    --rule       default|conservative|<JSON params>
    --dry-run

체결 원칙 (룩어헤드 방지):
    신호 발생: t일 종가 데이터 기준
    진입/청산: t+1 거래일 close_price
    → 미래 데이터는 절대 참조하지 않음
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from psycopg2.extras import execute_values

from ..db.connection import get_conn, get_cur
from .rules import RuleParams, DEFAULT_PARAMS, CONSERVATIVE_PARAMS

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PARQUET_DIR = ROOT / "data" / "parquet"


# ── 데이터 로더 ───────────────────────────────────────────────────────────────

def _load_parquet(parquet_dir: Path, start: Optional[date], end: Optional[date]) -> pd.DataFrame:
    """fact_daily_stock Parquet 로드. start/end 필터 적용.

    fact_daily_stock_pre2020(2026-06-23 도입 — KRX OPEN API 백필 + calc_derived.py
    동일 공식 재적용한 2015~2019 구간, fact_daily_stock과 완전히 동일한 스키마/dtype)도
    함께 스캔 — 이 store가 있으면 E3 백테스트(derived_trades/derived_leader_regime/
    derived_theme_daily)도 2015년부터 정상 계산된다."""
    stock_dirs = [parquet_dir / "fact_daily_stock", parquet_dir / "fact_daily_stock_pre2020"]
    parts = sorted(
        p for stock_dir in stock_dirs if stock_dir.exists()
        for p in stock_dir.iterdir() if p.is_dir()
    )

    dfs = []
    for part in parts:
        ym = part.name.split("=")[1]           # yearmonth=YYYYMM → YYYYMM
        ym_start = date(int(ym[:4]), int(ym[4:]), 1)
        last_day = (ym_start.replace(month=ym_start.month % 12 + 1, day=1)
                    if ym_start.month < 12
                    else ym_start.replace(year=ym_start.year + 1, month=1, day=1)) - timedelta(days=1)
        if start and last_day < start:
            continue
        if end and ym_start > end:
            continue
        df = pd.read_parquet(part / "data.parquet")
        dfs.append(df)

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"].astype(str)).dt.date

    if start:
        df = df[df["date"] >= start]
    if end:
        df = df[df["date"] <= end]

    # close_price float → int
    df["close_price"] = pd.to_numeric(df["close_price"], errors="coerce").round().astype("Int64")
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce").round().astype("Int64")
    df["theme1_amount"] = pd.to_numeric(df["theme1_amount"], errors="coerce")

    return df.sort_values(["date", "rank"]).reset_index(drop=True)


def _get_dataset_version(parquet_dir: Path) -> str:
    manifest_path = parquet_dir / "_manifest.json"
    if manifest_path.exists():
        with open(manifest_path, encoding="utf-8") as f:
            m = json.load(f)
        stock = m.get("fact_daily_stock", {})
        if stock:
            return max(v["max_date"] for v in stock.values() if v.get("max_date"))
    return date.today().isoformat()


# ── STEP 1: derived_theme_daily ──────────────────────────────────────────────

def compute_theme_daily(df: pd.DataFrame, params: RuleParams) -> pd.DataFrame:
    """
    각 (date, theme1)에 대해:
    - theme_amount: theme1_amount (테마 전체 거래대금, 같은 테마면 동일)
    - avg_change_pct: 테마 내 종목 평균 등락률
    - stock_count: 해당 날 top-N 내 테마 종목 수
    - leader: 가장 낮은 global rank 종목 (= 거래대금 최상위)
    - is_top_theme: 해당 날 theme_amount 최대 여부

    룩어헤드 없음: 모든 값이 당일(t) 관측값.
    """
    # theme1 없는 행 제외
    df = df[df["theme1"].notna()].copy()

    # 테마별 집계
    g = df.groupby(["date", "theme1"])

    agg = g.agg(
        theme_amount=("theme1_amount", "first"),   # 같은 테마는 동일 값
        avg_change_pct=("change_pct", "mean"),
        stock_count=("code", "count"),
    ).reset_index()

    # 리더: 해당 테마 내 global rank 최소 종목
    leader_idx = g["rank"].idxmin()
    leaders = df.loc[leader_idx, ["date", "theme1", "code", "name", "rank", "close_price"]].rename(
        columns={"code": "leader_code", "name": "leader_name",
                 "rank": "leader_rank", "close_price": "leader_close"}
    )

    result = agg.merge(leaders, on=["date", "theme1"], how="left")

    # is_top_theme: 날짜별 theme_amount 최대 테마
    max_amount = result.groupby("date")["theme_amount"].transform("max")
    result["is_top_theme"] = result["theme_amount"] == max_amount

    # 정수 변환
    for col in ["theme_amount", "leader_close"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round().astype("Int64")
    result["stock_count"] = result["stock_count"].astype("Int32")
    result["leader_rank"] = pd.to_numeric(result["leader_rank"], errors="coerce").round().astype("Int32")

    return result


# ── STEP 2: derived_leader_regime ────────────────────────────────────────────

def compute_regimes(theme_daily: pd.DataFrame, params: RuleParams) -> pd.DataFrame:
    """
    날짜 순 top 테마 시퀀스에서 연속 구간(레짐)을 추출.

    switch_threshold_pct > 0 이면:
      신규 테마 거래대금 / 현재 테마 거래대금 - 1 >= threshold 일 때만 교체.
    """
    top = theme_daily[theme_daily["is_top_theme"]].copy()
    top = top.sort_values("date").reset_index(drop=True)

    # 날짜별 정확히 하나의 top theme 보장 (동점 시 첫 번째)
    top = top.drop_duplicates("date", keep="first")

    # 교체 감지
    prev_theme = top["theme1"].shift(1)
    prev_amount = top["theme_amount"].shift(1).astype(float)
    curr_amount = top["theme_amount"].astype(float)

    if params.switch_threshold_pct > 0:
        ratio = (curr_amount - prev_amount) / prev_amount.replace(0, np.nan)
        switched = (top["theme1"] != prev_theme) & (ratio >= params.switch_threshold_pct / 100)
    else:
        switched = top["theme1"] != prev_theme

    switched.iloc[0] = True           # 첫 날은 항상 새 레짐
    top["regime_id_tmp"] = switched.cumsum()

    regimes = (
        top.groupby(["regime_id_tmp", "theme1"])
        .agg(
            start_date=("date", "min"),
            end_date=("date", "max"),
            avg_theme_amount=("theme_amount", "mean"),
            leader_code=("leader_code", "last"),   # 레짐 마지막 날의 리더
            leader_name=("leader_name", "last"),
        )
        .reset_index()
        .drop(columns=["regime_id_tmp"])
    )

    # 지속 거래일 계산 (날짜 세기)
    trading_dates = sorted(top["date"].unique())
    date_to_idx = {d: i for i, d in enumerate(trading_dates)}
    regimes["duration_days"] = regimes.apply(
        lambda r: date_to_idx.get(r["end_date"], 0) - date_to_idx.get(r["start_date"], 0) + 1,
        axis=1,
    )

    regimes["avg_theme_amount"] = regimes["avg_theme_amount"].round().astype("Int64")
    regimes = regimes.sort_values("start_date").reset_index(drop=True)

    # min_regime_days 필터 (짧은 레짐은 거래 제외하되 레짐 자체는 보존)
    return regimes


# ── STEP 3: derived_trades ───────────────────────────────────────────────────

def compute_trades(
    df: pd.DataFrame,
    regimes: pd.DataFrame,
    params: RuleParams,
    dataset_version: str,
) -> pd.DataFrame:
    """
    레짐별 거래 로그 생성.

    체결 원칙 (룩어헤드 방지):
      - signal_date = regime 시작일 (t일, 이 날 데이터로 신호 확정)
      - entry_date  = 다음 거래일 (t+1, 이 날 close_price로 체결)
      - exit_date   = 레짐 종료 다음 거래일 (t+1, 이 날 close_price로 체결)
      - stop_loss / take_profit: 당일 close를 확인 → 다음 날 close 체결
    """
    trading_dates = sorted(df["date"].unique())
    date_to_idx = {d: i for i, d in enumerate(trading_dates)}

    # 가격 룩업: (date, code) → close_price
    price_lut = df.dropna(subset=["close_price"]).set_index(["date", "code"])["close_price"]

    def next_date(d: date) -> Optional[date]:
        idx = date_to_idx.get(d, -1)
        if idx < 0 or idx + 1 >= len(trading_dates):
            return None
        return trading_dates[idx + 1]

    def get_price(d: date, code: str) -> Optional[int]:
        val = price_lut.get((d, code))
        if val is None or (hasattr(val, "__len__") and len(val) == 0):
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    # 인적분할/합병/상호변경 가드 (2026-06-23 발견): 보유기간 중 종목명이 바뀌면
    # 같은 종목코드라도 가격이 인위적으로 재평가되어(예: BGF리테일→BGF 인적분할 시
    # 79,100원→15,350원, -80.6%) 실제로는 보존된 가치(분할로 받은 신주 별도 보유)가
    # "손실"로 잘못 계산된다. 242건 중 1건이 누적수익률을 -85%→-23%로 왜곡시킨 사례
    # 확인 — 분할비율을 모르는 한 정확한 보정이 불가능하므로 해당 트레이드는 제외한다.
    def _name_changed(code: str, start_d: date, end_d: date) -> bool:
        if start_d is None or end_d is None:
            return False
        sub = df.loc[(df["code"] == code) & (df["date"] >= start_d) & (df["date"] <= end_d), "name"]
        return sub.dropna().nunique() > 1

    trades = []
    for _, regime in regimes.iterrows():
        if regime["duration_days"] < params.min_regime_days:
            continue

        code = regime["leader_code"]
        if pd.isna(code):
            continue

        # 신호일 = regime 시작일 (t)
        signal_date = regime["start_date"]
        entry_date = next_date(signal_date)
        if entry_date is None:
            continue

        entry_price = get_price(entry_date, code)
        if entry_price is None:
            continue

        # 청산 기준: regime 종료 다음 거래일
        exit_date_raw = next_date(regime["end_date"])
        exit_date = exit_date_raw
        exit_price = None
        exit_reason = "open"

        # 레짐 기간 거래일 목록
        regime_dates = [d for d in trading_dates
                        if regime["start_date"] <= d <= regime["end_date"]]

        # 손절/익절 확인 (t일 close로 확인 → t+1일 close 체결)
        for chk_date in regime_dates:
            chk_price = get_price(chk_date, code)
            if chk_price is None:
                continue
            ret_pct = (chk_price - entry_price) / entry_price * 100
            next_d = next_date(chk_date)
            if next_d is None:
                break

            if params.stop_loss_pct > 0 and ret_pct <= -params.stop_loss_pct:
                exit_date = next_d
                exit_price = get_price(next_d, code)
                exit_reason = "stop_loss"
                break
            if params.take_profit_pct > 0 and ret_pct >= params.take_profit_pct:
                exit_date = next_d
                exit_price = get_price(next_d, code)
                exit_reason = "take_profit"
                break
        else:
            # 손절/익절 미발동 → regime_end 또는 open
            if exit_date_raw:
                exit_price = get_price(exit_date_raw, code)
                exit_reason = "regime_end"

        # 보유기간 중 종목명 변경(분할/합병/상호변경) 시 가격이 인위적으로 재평가되어
        # 실제 보존된 가치가 손실로 잘못 계산되므로 해당 트레이드는 제외
        if exit_date and _name_changed(code, entry_date, exit_date):
            continue

        # 수익률 계산
        pnl_pct = None
        net_pnl_pct = None
        if exit_price and entry_price:
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            net_pnl_pct = pnl_pct - params.fee_pct

        hold_days = None
        if exit_date and entry_date:
            ed_idx = date_to_idx.get(exit_date, -1)
            en_idx = date_to_idx.get(entry_date, -1)
            if ed_idx >= 0 and en_idx >= 0:
                hold_days = ed_idx - en_idx

        trades.append({
            "code": code,
            "name": str(regime["leader_name"]) if pd.notna(regime["leader_name"]) else None,
            "theme1": regime["theme1"],
            "signal_date": signal_date,
            "entry_date": entry_date,
            "entry_price": int(entry_price) if entry_price else None,
            "exit_date": exit_date,
            "exit_price": int(exit_price) if exit_price else None,
            "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
            "fee_pct": params.fee_pct,
            "net_pnl_pct": round(net_pnl_pct, 4) if net_pnl_pct is not None else None,
            "hold_days": hold_days,
            "rule_version": params.rule_version,
            "dataset_version": dataset_version,
        })

    return pd.DataFrame(trades)


# ── DB 저장 ───────────────────────────────────────────────────────────────────

def _delete_derived(conn, start: date, rv: str) -> None:
    """recalc_start 이후 기존 파생 데이터 삭제 (재계산분으로 교체하기 전 정리).

    2026-06-18 발견한 버그: dv 인자는 항상 _get_dataset_version()으로 갓 계산한
    "이번 run의 새 버전"이라, 기존에 DB에 있는 행들은 전부 다른(과거) dataset_version을
    달고 있어 `dataset_version=%s` 조건이 매번 0건 매칭(no-op)됨 — 그 결과 같은
    기간이 재계산될 때마다 기존 행이 삭제되지 않고 새 dv로 또 INSERT만 돼서
    derived_theme_daily/derived_leader_regime/derived_trades에 같은 트레이드/레짐이
    dataset_version만 다른 채 계속 중복 누적됨(실측: derived_trades 380쌍 중복).
    dataset_version 조건을 빼고 날짜+rule_version만으로 그 구간의 모든 과거 버전을
    삭제해야 재계산분으로 깨끗하게 교체됨."""
    for table in ["derived_theme_daily", "derived_leader_regime", "derived_trades"]:
        date_col = "date" if table == "derived_theme_daily" else (
            "start_date" if table == "derived_leader_regime" else "entry_date"
        )
        with get_cur(conn) as cur:
            cur.execute(
                f"DELETE FROM {table} WHERE {date_col} >= %s AND rule_version=%s",
                (start, rv),
            )


def _insert_theme_daily(conn, df: pd.DataFrame, rv: str, dv: str) -> int:
    df = df.copy()
    df["rule_version"] = rv
    df["dataset_version"] = dv
    cols = ["date","theme1","theme_amount","avg_change_pct","stock_count",
            "leader_code","leader_name","leader_rank","leader_close",
            "is_top_theme","rule_version","dataset_version"]

    rows = []
    for _, r in df.iterrows():
        rows.append((
            r["date"], r["theme1"],
            None if pd.isna(r["theme_amount"]) else int(r["theme_amount"]),
            None if pd.isna(r["avg_change_pct"]) else float(r["avg_change_pct"]),
            None if pd.isna(r["stock_count"]) else int(r["stock_count"]),
            r["leader_code"] if pd.notna(r["leader_code"]) else None,
            r["leader_name"] if pd.notna(r["leader_name"]) else None,
            None if pd.isna(r["leader_rank"]) else int(r["leader_rank"]),
            None if pd.isna(r["leader_close"]) else int(r["leader_close"]),
            bool(r["is_top_theme"]),
            rv, dv,
        ))

    with get_cur(conn) as cur:
        execute_values(cur, f"""
            INSERT INTO derived_theme_daily
              ({",".join(cols)})
            VALUES %s
            ON CONFLICT DO NOTHING
        """, rows)
    return len(rows)


def _insert_regimes(conn, df: pd.DataFrame, rv: str, dv: str) -> list[int]:
    """Insert regimes and return list of DB-assigned regime_ids."""
    regime_ids = []
    with get_cur(conn) as cur:
        for _, r in df.iterrows():
            cur.execute("""
                INSERT INTO derived_leader_regime
                  (theme1, leader_code, leader_name, start_date, end_date,
                   duration_days, avg_theme_amount, rule_version, dataset_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING regime_id
            """, (
                r["theme1"],
                r["leader_code"] if pd.notna(r["leader_code"]) else None,
                r["leader_name"] if pd.notna(r["leader_name"]) else None,
                r["start_date"], r["end_date"],
                int(r["duration_days"]),
                None if pd.isna(r["avg_theme_amount"]) else int(r["avg_theme_amount"]),
                rv, dv,
            ))
            regime_ids.append(cur.fetchone()[0])
    return regime_ids


def _insert_trades(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = ["code","name","theme1","signal_date","entry_date","entry_price",
            "exit_date","exit_price","exit_reason","pnl_pct","fee_pct",
            "net_pnl_pct","hold_days","rule_version","dataset_version"]
    rows = [tuple(None if pd.isna(r[c]) else r[c] for c in cols)
            for _, r in df.iterrows()]
    with get_cur(conn) as cur:
        execute_values(cur, f"""
            INSERT INTO derived_trades ({",".join(cols)}) VALUES %s
        """, rows)
    return len(rows)


# ── 성과 리포트 ──────────────────────────────────────────────────────────────

def _print_report(trades_df: pd.DataFrame, params: RuleParams,
                  start: date, end: date) -> dict:
    closed = trades_df[trades_df["exit_reason"] != "open"].copy()
    if closed.empty:
        print("  거래 없음")
        return {}

    pnls = closed["net_pnl_pct"].dropna()
    wins = (pnls > 0).sum()
    total = len(pnls)

    # MDD
    cumret = (1 + pnls / 100).cumprod()
    running_max = cumret.cummax()
    drawdown = (cumret - running_max) / running_max * 100
    mdd = drawdown.min()

    avg_hold = closed["hold_days"].dropna().mean()

    report = {
        "기간": f"{start} ~ {end}",
        "총 거래": total,
        "승률": f"{wins/total*100:.1f}%" if total else "N/A",
        "평균 수익률": f"{pnls.mean():.2f}%",
        "누적 수익률": f"{(cumret.iloc[-1]-1)*100:.2f}%",
        "MDD": f"{mdd:.2f}%",
        "평균 보유일": f"{avg_hold:.1f}일",
        "수수료(왕복)": f"{params.fee_pct}%",
    }

    print(f"\n{'='*50}")
    print(f"백테스트 결과 ({params.rule_version})")
    print(f"{'='*50}")
    for k, v in report.items():
        print(f"  {k:<15}: {v}")
    print(f"{'='*50}")

    return report


# ── 메인 ─────────────────────────────────────────────────────────────────────

def run(
    parquet_dir: Path,
    params: RuleParams,
    start: Optional[date] = None,
    end: Optional[date] = None,
    dry_run: bool = False,
) -> dict:
    dataset_version = _get_dataset_version(parquet_dir)
    rv = params.rule_version
    dv = dataset_version

    print(f"\n[E3] 파생 테이블 계산")
    print(f"  rule_version={rv}, dataset_version={dv}")
    print(f"  기간: {start} ~ {end}")

    t0 = time.time()

    # 로드 (recalc 버퍼 포함)
    load_start = (datetime.combine(start, datetime.min.time()) - timedelta(days=params.lookback_days * 2)).date() if start else None
    print(f"\n[1] Parquet 로드 (from {load_start})...")
    df = _load_parquet(parquet_dir, load_start, end)
    if df.empty:
        print("  데이터 없음")
        return {}
    print(f"  {len(df):,}행, {df['date'].nunique()}거래일 ({df['date'].min()} ~ {df['date'].max()})")

    # STEP 1
    print("\n[2] derived_theme_daily 계산...")
    theme_daily = compute_theme_daily(df, params)
    print(f"  {len(theme_daily):,}행 ({theme_daily['theme1'].nunique()} 테마)")

    # STEP 2
    print("\n[3] derived_leader_regime 계산...")
    regimes = compute_regimes(theme_daily, params)
    print(f"  {len(regimes)}개 레짐 (평균 {regimes['duration_days'].mean():.1f}거래일)")

    # STEP 3
    print("\n[4] derived_trades 계산...")
    trades = compute_trades(df, regimes, params, dv)
    print(f"  {len(trades)}건 거래 신호")

    if dry_run:
        print("\n[DRY RUN] DB 저장 생략")
        _print_report(trades, params, df["date"].min(), df["date"].max())
        return {"dry_run": True, "trades": len(trades)}

    # DB 저장
    print("\n[5] DB 저장...")
    recalc_start = load_start or df["date"].min()
    with get_conn() as conn:
        _delete_derived(conn, recalc_start, rv)
        conn.commit()

        n_td = _insert_theme_daily(conn, theme_daily, rv, dv)
        conn.commit()

        regime_ids = _insert_regimes(conn, regimes, rv, dv)
        conn.commit()

        # regime_id 매핑
        if not trades.empty and regime_ids:
            trades_with_id = []
            for (_, regime_row), rid in zip(regimes.iterrows(), regime_ids):
                mask = (
                    (trades["theme1"] == regime_row["theme1"]) &
                    (trades["signal_date"] == regime_row["start_date"])
                )
                matched = trades[mask].copy()
                matched["regime_id"] = rid
                trades_with_id.append(matched)
            if trades_with_id:
                trades_final = pd.concat(trades_with_id, ignore_index=True)
            else:
                trades_final = trades.copy()
                trades_final["regime_id"] = None
        else:
            trades_final = trades.copy()
            trades_final["regime_id"] = None

        n_tr = _insert_trades(conn, trades_final)
        conn.commit()

    elapsed = time.time() - t0
    print(f"  derived_theme_daily: {n_td:,}행")
    print(f"  derived_leader_regime: {len(regime_ids)}행")
    print(f"  derived_trades: {n_tr}행")
    print(f"  elapsed: {elapsed:.1f}초")

    # 샘플 기간 리포트
    rpt_start = start or df["date"].min()
    rpt_end   = end   or df["date"].max()
    report = _print_report(trades, params, rpt_start, rpt_end)

    return {
        "n_theme_daily": n_td,
        "n_regimes": len(regime_ids),
        "n_trades": n_tr,
        "elapsed_sec": elapsed,
        "report": report,
    }


def main():
    parser = argparse.ArgumentParser(description="E3 파생 테이블 계산 + 백테스트")
    parser.add_argument("--parquet-dir", default=str(DEFAULT_PARQUET_DIR))
    parser.add_argument("--start-date", help="YYYY-MM-DD")
    parser.add_argument("--end-date",   help="YYYY-MM-DD")
    parser.add_argument("--rule", default="default",
                        choices=["default", "conservative"],
                        help="룰 프리셋")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    params = DEFAULT_PARAMS if args.rule == "default" else CONSERVATIVE_PARAMS

    start = date.fromisoformat(args.start_date) if args.start_date else None
    end   = date.fromisoformat(args.end_date)   if args.end_date   else None

    run(
        parquet_dir=Path(args.parquet_dir),
        params=params,
        start=start,
        end=end,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()

"""
E2: Parquet -> Postgres 증분 적재기
Usage:
    python -m ssen.db.load_parquet_to_postgres [OPTIONS]

Options:
    --parquet-dir PATH   Parquet 루트 (default: data/parquet)
    --months YYYYMM ...  특정 월만 적재 (미지정 시 manifest 기준 전체)
    --overlap POLICY     rebuild | new_only (default: rebuild)
    --dry-run            SQL 실행 없이 영향 파티션만 출력
"""
import argparse
import io
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from .connection import get_conn, get_cur
from .partitions import ensure_partitions_for_months

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PARQUET_DIR = ROOT / "data" / "parquet"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_manifest(parquet_dir: Path) -> dict:
    p = parquet_dir / "_manifest.json"
    if not p.exists():
        raise FileNotFoundError(f"manifest not found: {p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _get_loaded_months(conn) -> dict[str, str]:
    """etl_runs에서 성공적으로 로드된 파티션별 dataset_version 조회."""
    with get_cur(conn) as cur:
        cur.execute("""
            SELECT unnest(partitions) as ym, dataset_version
            FROM   etl_runs
            WHERE  status = 'done'
        """)
        rows = cur.fetchall()
    return {row[0]: row[1] for row in rows} if rows else {}


# ── COPY 기반 bulk loader ──────────────────────────────────────────────────────

def _to_nullable_int(series: pd.Series) -> pd.Series:
    """float 컬럼을 nullable Int64로 변환 (COPY 시 "123.0" 방지)."""
    return series.round().astype("Int64")


def _df_to_copy(conn, df: pd.DataFrame, table: str, columns: list[str]) -> int:
    """pandas DataFrame을 COPY로 bulk insert. 반환: 삽입 행수."""
    buf = io.StringIO()
    df[columns].to_csv(buf, index=False, header=False, na_rep="\\N", sep="\t",
                       date_format="%Y-%m-%d")
    buf.seek(0)
    with get_cur(conn) as cur:
        cur.copy_from(buf, table, columns=columns, null="\\N", sep="\t")
    return len(df)


# ── 테이블별 적재 로직 ────────────────────────────────────────────────────────

STOCK_COLS = [
    "date","rank","code","name","market",
    "base_price","close_price","change","change_pct",
    "volume_sum","volume_avg","amount_sum","amount_avg","shares",
    "vs_kospi_pct","mktcap","amount_vs_mktcap_pct","size_class",
    "contrib_score","contrib_rank",
    "theme1","theme1_rank","theme1_amount","theme1_pct",
    "theme2_rank","theme2","theme2_amount","theme2_pct","strength",
]

KOSPI_COLS = ["date","open","high","low","close","volume","change_rate"]
ADR_COLS   = ["date","index_name","up_count","down_count","flat_count","adr","is_verified"]


def _truncate_partition(conn, table: str, yearmonth: str) -> None:
    part = f"{table}_{yearmonth}"
    with get_cur(conn) as cur:
        cur.execute(f"TRUNCATE TABLE {part}")


def _load_stock_month(conn, parquet_dir: Path, ym: str) -> int:
    part_file = parquet_dir / "fact_daily_stock" / f"yearmonth={ym}" / "data.parquet"
    if not part_file.exists():
        return 0
    df = pd.read_parquet(part_file)
    df["date"] = pd.to_datetime(df["date"].astype(str)).dt.date
    # float → int 변환 (BIGINT 컬럼, NaN은 None)
    int_cols = ["base_price","close_price","change","volume_sum","volume_avg",
                "amount_sum","amount_avg","shares","mktcap","contrib_rank",
                "theme1_rank","theme1_amount","theme2_rank","theme2_amount"]
    for c in int_cols:
        if c in df.columns:
            df[c] = _to_nullable_int(df[c])
    df["rank"] = _to_nullable_int(df["rank"])

    _truncate_partition(conn, "fact_daily_stock", ym)
    return _df_to_copy(conn, df, f"fact_daily_stock_{ym}", STOCK_COLS)


def _load_kospi_month(conn, parquet_dir: Path, ym: str) -> int:
    part_file = parquet_dir / "fact_kospi" / f"yearmonth={ym}" / "data.parquet"
    if not part_file.exists():
        return 0
    df = pd.read_parquet(part_file)
    df["date"] = pd.to_datetime(df["date"].astype(str)).dt.date
    df["volume"] = _to_nullable_int(df["volume"])
    _truncate_partition(conn, "fact_kospi_index", ym)
    return _df_to_copy(conn, df, f"fact_kospi_index_{ym}", KOSPI_COLS)


def _load_adr_month(conn, parquet_dir: Path, ym: str) -> int:
    part_file = parquet_dir / "fact_adr" / f"yearmonth={ym}" / "data.parquet"
    if not part_file.exists():
        return 0
    df = pd.read_parquet(part_file)
    df["date"] = pd.to_datetime(df["date"].astype(str)).dt.date
    for c in ["up_count","down_count","flat_count"]:
        df[c] = _to_nullable_int(df[c])
    if "is_verified" not in df.columns:
        df["is_verified"] = True  # 005_adr_verified_flag.sql 이전에 적재된 구 파티션 호환
    _truncate_partition(conn, "fact_adr", ym)
    return _df_to_copy(conn, df, f"fact_adr_{ym}", ADR_COLS)


def _load_dim_tables(conn, parquet_dir: Path) -> None:
    """dim_theme, dim_stock, map_stock_theme 로드 (TRUNCATE + reload)."""
    theme_file = parquet_dir / "dim_theme" / "data.parquet"
    if not theme_file.exists():
        return
    df = pd.read_parquet(theme_file)
    df["code"] = df["code"].str.zfill(6)
    df["shares"] = _to_nullable_int(df["shares"])

    with get_cur(conn) as cur:
        cur.execute("TRUNCATE TABLE map_stock_theme CASCADE")
        cur.execute("TRUNCATE TABLE dim_theme CASCADE")
    _df_to_copy(conn, df, "dim_theme", ["name","code","theme1","theme2","shares"])

    # map_stock_theme 구성
    rows = []
    for _, row in df.iterrows():
        if pd.notna(row.get("theme1")):
            rows.append((row["code"], 1, row["theme1"]))
        if pd.notna(row.get("theme2")):
            rows.append((row["code"], 2, row["theme2"]))
    if rows:
        with get_cur(conn) as cur:
            execute_values(cur,
                "INSERT INTO map_stock_theme(code,theme_type,theme_name) VALUES %s "
                "ON CONFLICT DO NOTHING", rows)
    print(f"  OK dim_theme: {len(df)}행, map_stock_theme: {len(rows)}행")


def _upsert_dim_stock(conn, parquet_dir: Path, yearmonths: list[str]) -> None:
    """fact_daily_stock에서 dim_stock 최신 정보 upsert."""
    records = []
    for ym in yearmonths:
        f = parquet_dir / "fact_daily_stock" / f"yearmonth={ym}" / "data.parquet"
        if f.exists():
            df = pd.read_parquet(f, columns=["code","name","market","size_class","shares"])
            df = df.drop_duplicates("code")
            records.append(df)
    if not records:
        return
    df_all = pd.concat(records).drop_duplicates("code")
    with get_cur(conn) as cur:
        execute_values(cur, """
            INSERT INTO dim_stock(code, name, market, size_class, shares, updated_at)
            VALUES %s
            ON CONFLICT (code) DO UPDATE
              SET name=EXCLUDED.name, market=EXCLUDED.market,
                  size_class=EXCLUDED.size_class, shares=EXCLUDED.shares,
                  updated_at=now()
        """, [
            (r["code"], r["name"], r.get("market"), r.get("size_class"),
             int(r["shares"]) if pd.notna(r.get("shares")) else None,
             datetime.now(timezone.utc))
            for _, r in df_all.iterrows()
        ])
    print(f"  OK dim_stock upsert: {len(df_all)}종목")


# ── ETL run 기록 ──────────────────────────────────────────────────────────────

def _start_run(conn, source_file: str, months: list[str]) -> int:
    with get_cur(conn) as cur:
        cur.execute("""
            INSERT INTO etl_runs(started_at, input_files, partitions, status)
            VALUES (now(), %s, %s, 'running')
            RETURNING run_id
        """, ([source_file], months))
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def _finish_run(conn, run_id: int, min_date: str, max_date: str,
                months: list[str], total_rows: int, status: str = "done",
                dataset_version: str | None = None) -> None:
    # dataset_version은 "이번 run이 건드린 범위"가 아니라 "데이터셋 전체의 최신 상태"를
    # 의미해야 함. 부분 재적재(months=[...]) 시 max_date(이번 run 범위)로 덮어쓰면
    # 더 최신 데이터가 이미 있어도 dataset_version이 과거로 후퇴하는 버그가 있었음
    # (2026-06-17 발견 — load(months=['202203','202204']) 호출 후 API가 보고하는
    # dataset_version이 '2022-04-29'로 깨짐).
    if dataset_version is None:
        dataset_version = max_date
    with get_cur(conn) as cur:
        cur.execute("""
            UPDATE etl_runs
            SET finished_at    = now(),
                min_date       = %s,
                max_date       = %s,
                dataset_version= %s,
                partitions     = %s,
                total_rows     = %s,
                status         = %s
            WHERE run_id = %s
        """, (min_date, max_date, dataset_version, months, total_rows, status, run_id))
    conn.commit()


# ── 메인 로더 ─────────────────────────────────────────────────────────────────

def load(parquet_dir: Path, months: list[str] | None = None,
         overlap_policy: str = "rebuild", dry_run: bool = False) -> dict:
    manifest = _load_manifest(parquet_dir)
    all_months = sorted(manifest["fact_daily_stock"].keys())
    source_file = manifest.get("last_source_file", "unknown")

    # 적재 대상 월 결정
    if months:
        target_months = [m for m in months if m in all_months]
    elif overlap_policy == "new_only":
        with get_conn() as conn:
            loaded = _get_loaded_months(conn)
        target_months = [m for m in all_months if m not in loaded]
    else:  # rebuild
        target_months = all_months

    if not target_months:
        print("적재할 새 파티션 없음.")
        return {"months": [], "total_rows": 0}

    print(f"적재 대상: {len(target_months)}개월 | {target_months[0]} ~ {target_months[-1]}")
    print(f"overlap 정책: {overlap_policy}")

    if dry_run:
        print("[DRY RUN] 영향 파티션:", target_months)
        return {"months": target_months, "total_rows": 0, "dry_run": True}

    # 파티션 자동 생성
    print("\n[1] 파티션 생성...")
    ensure_partitions_for_months(target_months)

    # ETL run 시작 기록
    with get_conn() as conn:
        run_id = _start_run(conn, source_file, target_months)

    total_rows = 0
    t0 = time.time()

    print("\n[2] 데이터 적재...")
    with get_conn() as conn:
        try:
            # dim 테이블 (전체 reload)
            _load_dim_tables(conn, parquet_dir)
            _upsert_dim_stock(conn, parquet_dir, target_months[-6:])  # 최근 6개월
            conn.commit()

            # fact 테이블 (월별 TRUNCATE+COPY)
            for ym in target_months:
                t_ym = time.time()
                n_stock = _load_stock_month(conn, parquet_dir, ym)
                n_kospi = _load_kospi_month(conn, parquet_dir, ym)
                n_adr   = _load_adr_month(conn, parquet_dir, ym)
                conn.commit()
                total_rows += n_stock
                elapsed_ym = time.time() - t_ym
                print(f"  OK {ym}: stock={n_stock:,} kospi={n_kospi} adr={n_adr} ({elapsed_ym:.1f}s)")

            # 최소/최대 날짜 산출 (이번 run 범위 — 감사/기록용)
            stock_meta = manifest["fact_daily_stock"]
            loaded_meta = {m: stock_meta[m] for m in target_months if m in stock_meta}
            min_date = min(m["min_date"] for m in loaded_meta.values() if m["min_date"])
            max_date = max(m["max_date"] for m in loaded_meta.values() if m["max_date"])
            # dataset_version은 manifest 전체(all_months) 기준 — 부분 재적재로 후퇴하지 않게
            global_max_date = max(m["max_date"] for m in stock_meta.values() if m["max_date"])

            _finish_run(conn, run_id, min_date, max_date, target_months, total_rows,
                       dataset_version=global_max_date)

        except Exception as e:
            conn.rollback()  # 실패한 트랜잭션 먼저 롤백
            _finish_run(conn, run_id, None, None, target_months, 0, "failed")
            raise

    elapsed = time.time() - t0
    print(f"\n총 {total_rows:,}행 적재 완료 ({elapsed:.1f}초) | run_id={run_id}")
    return {"run_id": run_id, "months": target_months,
            "total_rows": total_rows, "elapsed_sec": elapsed}


def main():
    parser = argparse.ArgumentParser(description="Parquet -> Postgres 증분 적재기")
    parser.add_argument("--parquet-dir", default=str(DEFAULT_PARQUET_DIR))
    parser.add_argument("--months", nargs="*", help="YYYYMM 목록 (예: 202401 202402)")
    parser.add_argument("--overlap", choices=["rebuild","new_only"], default="rebuild")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load(
        parquet_dir=Path(args.parquet_dir),
        months=args.months or None,
        overlap_policy=args.overlap,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()

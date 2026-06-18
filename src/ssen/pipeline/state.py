"""
E7: Watermark / Manifest / Dataset-version 관리.

watermark = 마지막으로 성공한 파이프라인의 max_date.
이 값보다 큰 날짜를 가진 incoming 파일이 있으면 '신규 데이터'로 간주.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[3]
PARQUET_DIR = ROOT / "data" / "parquet"
MANIFEST_PATH = PARQUET_DIR / "_manifest.json"


# ── Parquet manifest ──────────────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"fact_daily_stock": {}}


def get_manifest_max_date() -> Optional[date]:
    m = load_manifest()
    dates = [v["max_date"] for v in m.get("fact_daily_stock", {}).values()
             if v.get("max_date")]
    return date.fromisoformat(max(dates)) if dates else None


def get_manifest_min_date() -> Optional[date]:
    m = load_manifest()
    dates = [v["min_date"] for v in m.get("fact_daily_stock", {}).values()
             if v.get("min_date")]
    return date.fromisoformat(min(dates)) if dates else None


def get_manifest_months() -> set[str]:
    m = load_manifest()
    return set(m.get("fact_daily_stock", {}).keys())


def get_manifest_checksum(yearmonth: str) -> Optional[str]:
    m = load_manifest()
    entry = m.get("fact_daily_stock", {}).get(yearmonth)
    return entry.get("checksum_md5") if entry else None


# ── DB watermark (etl_runs 기반) ──────────────────────────────────────────────

def get_db_watermark() -> Optional[date]:
    """DB의 etl_runs에서 마지막 성공 run의 max_date 반환."""
    try:
        from ssen.db.connection import get_conn, get_cur
        with get_conn() as conn:
            with get_cur(conn) as cur:
                cur.execute("""
                    SELECT max_date FROM etl_runs
                    WHERE status = 'done' AND max_date IS NOT NULL
                    ORDER BY finished_at DESC LIMIT 1
                """)
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def get_pipeline_last_success() -> Optional[dict]:
    """pipeline_runs에서 마지막 성공 run 정보 반환."""
    try:
        from ssen.db.connection import get_conn, get_cur
        with get_conn() as conn:
            with get_cur(conn) as cur:
                cur.execute("""
                    SELECT run_id, new_max_date, dataset_version, finished_at
                    FROM pipeline_runs
                    WHERE status = 'done'
                    ORDER BY finished_at DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    return {"run_id": row[0], "max_date": row[1],
                            "dataset_version": row[2], "finished_at": row[3]}
    except Exception:
        pass
    return None


# ── Incoming 파일 분석 ────────────────────────────────────────────────────────

def inspect_incoming_file(xlsx_path: Path) -> dict:
    """xlsx 파일의 날짜 범위와 행수를 빠르게 읽어서 반환."""
    import pandas as pd
    xl = pd.ExcelFile(xlsx_path, engine="openpyxl")
    df = pd.read_excel(xl, sheet_name="거래대금", usecols=["날짜"], dtype=str)
    dates = pd.to_datetime(df["날짜"], errors="coerce").dropna()
    return {
        "file": xlsx_path.name,
        "rows": len(df),
        "min_date": str(dates.min().date()) if len(dates) else None,
        "max_date": str(dates.max().date()) if len(dates) else None,
    }


def compute_affected_months(new_min: date, new_max: date) -> list[str]:
    """new_min ~ new_max 범위에 걸치는 yearmonth 파티션 목록."""
    months = []
    y, m = new_min.year, new_min.month
    end_y, end_m = new_max.year, new_max.month
    while (y, m) <= (end_y, end_m):
        months.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m = 1; y += 1
    return months

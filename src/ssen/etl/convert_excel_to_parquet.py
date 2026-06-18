"""
E1: Excel -> Parquet ETL
Usage:
    python -m ssen.etl.convert_excel_to_parquet [OPTIONS] [INPUT_FILES...]

Options:
    --input-dir PATH       Scan this dir for *.xlsx (default: data/incoming)
    --output-dir PATH      Parquet root (default: data/parquet)
    --overlap POLICY       new_only | rebuild (default: rebuild)
    --dry-run              Print affected partitions without writing
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[3]  # project root
DEFAULT_INPUT_DIR = ROOT / "data" / "incoming"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "parquet"

# ── Schema definitions ──────────────────────────────────────────────────────

FACT_STOCK_SCHEMA = pa.schema([
    pa.field("date",                  pa.date32()),
    pa.field("rank",                  pa.int32()),
    pa.field("code",                  pa.string()),
    pa.field("name",                  pa.string()),
    pa.field("market",                pa.string()),
    pa.field("base_price",            pa.float64()),
    pa.field("close_price",           pa.float64()),
    pa.field("change",                pa.float64()),
    pa.field("change_pct",            pa.float64()),
    pa.field("volume_sum",            pa.float64()),
    pa.field("volume_avg",            pa.float64()),
    pa.field("amount_sum",            pa.float64()),
    pa.field("amount_avg",            pa.float64()),
    pa.field("shares",                pa.float64()),
    pa.field("vs_kospi_pct",          pa.float64()),
    pa.field("mktcap",                pa.float64()),
    pa.field("amount_vs_mktcap_pct",  pa.float64()),
    pa.field("size_class",            pa.string()),
    pa.field("contrib_score",         pa.float64()),
    pa.field("contrib_rank",          pa.float64()),
    pa.field("theme1",                pa.string()),
    pa.field("theme1_rank",           pa.float64()),
    pa.field("theme1_amount",         pa.float64()),
    pa.field("theme1_pct",            pa.float64()),
    pa.field("theme2_rank",           pa.float64()),
    pa.field("theme2",                pa.string()),
    pa.field("theme2_amount",         pa.float64()),
    pa.field("theme2_pct",            pa.float64()),
    pa.field("strength",              pa.string()),
])

FACT_KOSPI_SCHEMA = pa.schema([
    pa.field("date",        pa.date32()),
    pa.field("open",        pa.float64()),
    pa.field("high",        pa.float64()),
    pa.field("low",         pa.float64()),
    pa.field("close",       pa.float64()),
    pa.field("volume",      pa.float64()),
    pa.field("change_rate", pa.float64()),
])

FACT_ADR_SCHEMA = pa.schema([
    pa.field("date",        pa.date32()),
    pa.field("index_name",  pa.string()),
    pa.field("up_count",    pa.int32()),
    pa.field("down_count",  pa.int32()),
    pa.field("flat_count",  pa.int32()),
    pa.field("adr",         pa.float64()),
    pa.field("is_verified", pa.bool_()),  # E9: 키움 당일수집 검증 전 임시값 플래그
])

DIM_THEME_SCHEMA = pa.schema([
    pa.field("name",   pa.string()),
    pa.field("code",   pa.string()),
    pa.field("theme1", pa.string()),
    pa.field("theme2", pa.string()),
    pa.field("shares", pa.float64()),
])


# ── Readers ──────────────────────────────────────────────────────────────────

def _read_fact_stock(xl: pd.ExcelFile) -> pd.DataFrame:
    df = pd.read_excel(xl, sheet_name="거래대금", dtype={"종목코드": str})
    df = df.rename(columns={
        "날짜": "date", "순위": "rank", "종목코드": "code", "종목명": "name",
        "시장구분": "market", "시작일기준가": "base_price", "종료일종가": "close_price",
        "대비": "change", "등락률": "change_pct",
        "거래량_합계": "volume_sum", "거래량_일평균": "volume_avg",
        "거래대금_합계": "amount_sum", "거래대금_일평균": "amount_avg",
        "상장주식수": "shares", "코스피 대비 등락률": "vs_kospi_pct",
        "시가총액": "mktcap", "시총 대비 거래대금 증가율": "amount_vs_mktcap_pct",
        "규모": "size_class", "기여점수": "contrib_score", "기여도순위": "contrib_rank",
        "테마(1차)": "theme1", "테마(1차) 순위": "theme1_rank",
        "테마(1차) 거래대금": "theme1_amount", "테마(1차) 등락률": "theme1_pct",
        "테마(2차) 순위": "theme2_rank", "테마(2차)": "theme2",
        "테마(2차) 거래대금": "theme2_amount", "테마(2차) 등락률": "theme2_pct",
        "강약 판정": "strength",
    })
    df["code"] = df["code"].str.zfill(6)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    # 숫자 컬럼은 모두 float64로 (nullable 처리)
    num_cols = ["base_price","close_price","change","volume_sum","volume_avg",
                "amount_sum","amount_avg","shares","mktcap","contrib_rank",
                "theme1_rank","theme1_amount","theme2_rank","theme2_amount"]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


KOSPI_VOLUME_SANITY_MIN = 1_000_000  # 코스피 전체 거래량은 항상 수억대 — 이 미만이면 단위 누락 의심


def _read_fact_kospi(xl: pd.ExcelFile) -> pd.DataFrame:
    df = pd.read_excel(xl, sheet_name="코스피시세")
    df = df.rename(columns={
        "Date": "date", "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume", "Increase rate": "change_rate",
    })
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    # sanity check: 코스피 Volume이 100만 미만이면 천주 단위 누락 의심 (2026-06-17 실측 발견 —
    # docs/perf_report_kospi_volume_fix.md 참조, 정상 범위는 항상 수억대)
    suspicious = df[df["volume"] < KOSPI_VOLUME_SANITY_MIN]
    if not suspicious.empty:
        bad_dates = suspicious["date"].astype(str).tolist()
        print(f"  [경고] 코스피 Volume 단위 누락 의심: {len(suspicious)}건 "
              f"(100만 미만, 예: {bad_dates[:5]}{'...' if len(bad_dates) > 5 else ''})")

    return df


def _read_fact_adr(xl: pd.ExcelFile) -> pd.DataFrame:
    df = pd.read_excel(xl, sheet_name="상승하락비율")
    df = df.rename(columns={
        "날짜": "date", "지수": "index_name",
        "상승종목수": "up_count", "하락종목수": "down_count",
        "보합종목수": "flat_count", "하락 대비 상승비율": "adr",
    })
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ["up_count", "down_count", "flat_count"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int32")
    df["adr"] = pd.to_numeric(df["adr"], errors="coerce")
    if "is_verified" not in df.columns:
        df["is_verified"] = True  # 마스터 xlsx(data.go.kr 등) 경유 데이터는 항상 확정치로 간주
    df["is_verified"] = df["is_verified"].fillna(True).astype(bool)
    return df


def _read_dim_theme(xl: pd.ExcelFile) -> pd.DataFrame:
    df = pd.read_excel(xl, sheet_name="테마", dtype={"종목코드": str})
    df = df.rename(columns={
        "종목명": "name", "종목코드": "code",
        "테마(1차)": "theme1", "테마(2차)": "theme2", "상장주식수": "shares",
    })
    df["code"] = df["code"].str.zfill(6)
    df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    return df


# ── Parquet writers ──────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checksum(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _to_float(df: pd.DataFrame) -> pd.DataFrame:
    """Int64/Int32 nullable → float64 for pyarrow compatibility."""
    out = df.copy()
    for col in out.columns:
        if str(out[col].dtype) in ("Int64", "Int32", "Int16"):
            out[col] = out[col].astype("float64")
    return out


def _write_partition(df: pd.DataFrame, out_dir: Path, schema: pa.Schema,
                     yearmonth: str, date_col: str = "date") -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "data.parquet"
    table = pa.Table.from_pandas(_to_float(df), schema=schema, safe=False)
    pq.write_table(table, out_file, compression="snappy")
    dates = df[date_col].dropna()
    return {
        "yearmonth": yearmonth,
        "rowcount": len(df),
        "min_date": str(min(dates)) if len(dates) else None,
        "max_date": str(max(dates)) if len(dates) else None,
        "file_bytes": out_file.stat().st_size,
        "checksum_md5": _checksum(out_file),
        "written_at": _now_utc(),
    }


def _write_nonpartitioned(df: pd.DataFrame, out_dir: Path,
                          schema: pa.Schema) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "data.parquet"
    table = pa.Table.from_pandas(_to_float(df), schema=schema, safe=False)
    pq.write_table(table, out_file, compression="snappy")
    return {
        "rowcount": len(df),
        "file_bytes": out_file.stat().st_size,
        "checksum_md5": _checksum(out_file),
        "written_at": _now_utc(),
    }


def _yearmonth(df: pd.DataFrame, date_col: str = "date") -> pd.Series:
    return (pd.to_datetime(df[date_col].astype(str))
            .dt.to_period("M").astype(str).str.replace("-", ""))


# ── Manifest ─────────────────────────────────────────────────────────────────

def _load_manifest(path: Path) -> dict:
    defaults = {"fact_daily_stock": {}, "fact_kospi": {}, "fact_adr": {},
                "dim_theme": {}, "snapshots": []}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    return defaults


def _save_manifest(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


# ── Main conversion ───────────────────────────────────────────────────────────

def convert_file(xlsx_path: Path, output_dir: Path,
                 overlap_policy: str = "rebuild", dry_run: bool = False) -> dict:
    print(f"\n[convert] {xlsx_path.name}")
    t0 = time.time()
    xl = pd.ExcelFile(xlsx_path, engine="openpyxl")

    manifest = _load_manifest(output_dir / "_manifest.json")
    result: dict[str, Any] = {"source_file": xlsx_path.name,
                               "fact_daily_stock": [], "affected_partitions": []}

    # ── 1. fact_daily_stock ──────────────────────────────────────────────────
    print("  읽는 중: 거래대금...", flush=True)
    df_stock = _read_fact_stock(xl)
    df_stock["_ym"] = _yearmonth(df_stock)
    incoming_months = sorted(df_stock["_ym"].unique())

    existing = set(manifest["fact_daily_stock"].keys())
    overlap  = [m for m in incoming_months if m in existing]
    new_only = [m for m in incoming_months if m not in existing]

    print(f"  fact_daily_stock: {len(df_stock):,}행, {len(incoming_months)}개월")
    print(f"  기존={len(existing)}개월 | overlap={len(overlap)}개월 | 신규={len(new_only)}개월")
    if overlap:
        print(f"  overlap 정책={overlap_policy}: {overlap[:3]}{'...' if len(overlap)>3 else ''}")
        result["affected_partitions"] = overlap

    if dry_run:
        print("  [DRY RUN] 파일 미생성")
        return result

    months_to_write = incoming_months if overlap_policy == "rebuild" else new_only
    for ym in months_to_write:
        part = df_stock[df_stock["_ym"] == ym].drop(columns=["_ym"]).copy()
        meta = _write_partition(part, output_dir / "fact_daily_stock" / f"yearmonth={ym}",
                                FACT_STOCK_SCHEMA, ym)
        manifest["fact_daily_stock"][ym] = meta
        result["fact_daily_stock"].append(meta)
        print(f"  OK yearmonth={ym}: {meta['rowcount']:,}행, {meta['file_bytes']//1024}KB")

    # ── 2. fact_kospi ────────────────────────────────────────────────────────
    print("  읽는 중: 코스피시세...", flush=True)
    df_kospi = _read_fact_kospi(xl)
    df_kospi["_ym"] = _yearmonth(df_kospi)
    for ym in sorted(df_kospi["_ym"].unique()):
        part = df_kospi[df_kospi["_ym"] == ym].drop(columns=["_ym"]).copy()
        meta = _write_partition(part, output_dir / "fact_kospi" / f"yearmonth={ym}",
                                FACT_KOSPI_SCHEMA, ym)
        manifest["fact_kospi"][ym] = meta
    print(f"  OK fact_kospi: {len(df_kospi):,}행, {df_kospi['_ym'].nunique()}개월")

    # ── 3. fact_adr (상승하락비율) ────────────────────────────────────────────
    print("  읽는 중: 상승하락비율...", flush=True)
    df_adr = _read_fact_adr(xl)
    df_adr["_ym"] = _yearmonth(df_adr)
    for ym in sorted(df_adr["_ym"].unique()):
        part = df_adr[df_adr["_ym"] == ym].drop(columns=["_ym"]).copy()
        meta = _write_partition(part, output_dir / "fact_adr" / f"yearmonth={ym}",
                                FACT_ADR_SCHEMA, ym)
        manifest["fact_adr"][ym] = meta
    print(f"  OK fact_adr: {len(df_adr):,}행, {df_adr['_ym'].nunique()}개월")

    # ── 4. dim_theme ─────────────────────────────────────────────────────────
    print("  읽는 중: 테마...", flush=True)
    df_theme = _read_dim_theme(xl)
    meta_theme = _write_nonpartitioned(df_theme, output_dir / "dim_theme", DIM_THEME_SCHEMA)
    manifest["dim_theme"] = meta_theme
    print(f"  OK dim_theme: {len(df_theme):,}행")

    # ── manifest 저장 ─────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    manifest["last_updated_at"] = _now_utc()
    manifest["last_source_file"] = xlsx_path.name
    _save_manifest(manifest, output_dir / "_manifest.json")

    # 요약
    stock_meta = manifest["fact_daily_stock"]
    total_rows  = sum(m["rowcount"] for m in stock_meta.values())
    total_bytes = sum(m["file_bytes"] for m in stock_meta.values())
    min_date = min(m["min_date"] for m in stock_meta.values() if m["min_date"])
    max_date = max(m["max_date"] for m in stock_meta.values() if m["max_date"])
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"  fact_daily_stock: {total_rows:,}행, {len(stock_meta)}개월, {total_bytes/1024/1024:.1f}MB")
    print(f"  날짜 범위: {min_date} ~ {max_date}")
    print(f"  변환 시간: {elapsed:.1f}초")
    print(f"  manifest: {output_dir / '_manifest.json'}")
    print(f"{'='*60}")

    result["elapsed_sec"] = elapsed
    return result


def main():
    parser = argparse.ArgumentParser(description="Excel -> Parquet ETL (E1)")
    parser.add_argument("files", nargs="*", help="입력 xlsx 파일 (지정 시 --input-dir 무시)")
    parser.add_argument("--input-dir",  default=str(DEFAULT_INPUT_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--overlap", choices=["rebuild", "new_only"], default="rebuild")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    if args.files:
        xlsx_files = [Path(f) for f in args.files]
    else:
        input_dir = Path(args.input_dir)
        xlsx_files = sorted(input_dir.glob("*.xlsx"))
        if not xlsx_files:
            print(f"No .xlsx files in {input_dir}")
            sys.exit(1)

    print(f"입력 파일: {[f.name for f in xlsx_files]}")
    print(f"출력 디렉토리: {output_dir}")
    print(f"Overlap 정책: {args.overlap}")

    for xlsx in xlsx_files:
        convert_file(xlsx, output_dir, overlap_policy=args.overlap, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

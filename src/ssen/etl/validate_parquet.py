"""
E1: Parquet Validation
Usage:
    python -m ssen.etl.validate_parquet [--output-dir PATH] [--report-format md|json]
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = ROOT / "data" / "parquet"
DOCS_DIR = ROOT / "docs"

FACT_STOCK_REQUIRED = ["date", "rank", "code", "name", "market"]
UNIQUE_KEY_COLS = ["date", "code", "rank"]


def _load_parquet_dir(table_dir: Path) -> pd.DataFrame:
    files = list(table_dir.rglob("*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def validate_fact_stock(parquet_root: Path) -> dict:
    table_dir = parquet_root / "fact_daily_stock"
    if not table_dir.exists():
        return {"status": "MISSING", "error": "fact_daily_stock dir not found"}

    partitions = sorted([d for d in table_dir.iterdir() if d.is_dir()])
    report = {"partitions": [], "summary": {}}
    all_dfs = []
    errors = []

    for part_dir in partitions:
        pq_file = part_dir / "data.parquet"
        if not pq_file.exists():
            errors.append(f"{part_dir.name}: data.parquet missing")
            continue
        df = pd.read_parquet(pq_file)
        n = len(df)
        dup_count = df[UNIQUE_KEY_COLS].duplicated().sum() if all(c in df.columns for c in UNIQUE_KEY_COLS) else -1
        null_counts = {c: int(df[c].isna().sum()) for c in FACT_STOCK_REQUIRED if c in df.columns}
        part_info = {
            "partition": part_dir.name,
            "rows": n,
            "duplicates_on_key": int(dup_count),
            "null_counts": null_counts,
        }
        if "date" in df.columns:
            dates = pd.to_datetime(df["date"].astype(str))
            part_info["min_date"] = str(dates.min().date())
            part_info["max_date"] = str(dates.max().date())
        if dup_count > 0:
            errors.append(f"{part_dir.name}: {dup_count} duplicate keys")
        for col, cnt in null_counts.items():
            if cnt > 0:
                errors.append(f"{part_dir.name}: {col} has {cnt} nulls")
        report["partitions"].append(part_info)
        all_dfs.append(df)

    if all_dfs:
        df_all = pd.concat(all_dfs, ignore_index=True)
        dates = pd.to_datetime(df_all["date"].astype(str))
        total_dup = df_all[UNIQUE_KEY_COLS].duplicated().sum() if all(c in df_all.columns for c in UNIQUE_KEY_COLS) else -1
        report["summary"] = {
            "total_rows": len(df_all),
            "total_partitions": len(partitions),
            "min_date": str(dates.min().date()),
            "max_date": str(dates.max().date()),
            "unique_dates": int(dates.dt.date.nunique()),
            "unique_codes": int(df_all["code"].nunique()) if "code" in df_all.columns else -1,
            "total_duplicates_on_key": int(total_dup),
        }

    report["errors"] = errors
    report["status"] = "PASS" if not errors else "FAIL"
    return report


def validate_fact_kospi(parquet_root: Path) -> dict:
    table_dir = parquet_root / "fact_kospi"
    if not table_dir.exists():
        return {"status": "MISSING", "error": "fact_kospi dir not found"}
    df = _load_parquet_dir(table_dir)
    if df.empty:
        return {"status": "EMPTY"}
    dates = pd.to_datetime(df["date"].astype(str))
    dup = df["date"].duplicated().sum()
    return {
        "status": "PASS" if dup == 0 else "FAIL",
        "rows": len(df),
        "min_date": str(dates.min().date()),
        "max_date": str(dates.max().date()),
        "duplicate_dates": int(dup),
    }


def validate_dim_theme(parquet_root: Path) -> dict:
    table_dir = parquet_root / "dim_theme"
    pq_file = table_dir / "data.parquet"
    if not pq_file.exists():
        return {"status": "MISSING"}
    df = pd.read_parquet(pq_file)
    dup = df["code"].duplicated().sum() if "code" in df.columns else -1
    return {
        "status": "PASS" if dup == 0 else "WARN",
        "rows": len(df),
        "duplicate_codes": int(dup),
        "unique_theme1": int(df["theme1"].nunique()) if "theme1" in df.columns else -1,
    }


def compute_parquet_size(parquet_root: Path) -> dict:
    total_bytes = sum(f.stat().st_size for f in parquet_root.rglob("*.parquet"))
    manifest_bytes = 0
    manifest_path = parquet_root / "_manifest.json"
    if manifest_path.exists():
        manifest_bytes = manifest_path.stat().st_size
    return {
        "total_parquet_mb": round(total_bytes / 1024 / 1024, 2),
        "manifest_kb": round(manifest_bytes / 1024, 1),
    }


def generate_md_report(results: dict, parquet_root: Path) -> str:
    stock = results["fact_daily_stock"]
    kospi = results["fact_kospi"]
    theme = results["dim_theme"]
    sizes = results["sizes"]
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# E1 Validation Report",
        f"",
        f"Generated: {now}",
        f"",
        f"## fact_daily_stock",
        f"",
        f"| 항목 | 값 |",
        f"|------|-----|",
        f"| Status | {stock.get('status','?')} |",
        f"| Total rows | {stock.get('summary',{}).get('total_rows','?'):,} |",
        f"| Partitions (months) | {stock.get('summary',{}).get('total_partitions','?')} |",
        f"| Date min | {stock.get('summary',{}).get('min_date','?')} |",
        f"| Date max | {stock.get('summary',{}).get('max_date','?')} |",
        f"| Unique dates | {stock.get('summary',{}).get('unique_dates','?')} |",
        f"| Unique codes | {stock.get('summary',{}).get('unique_codes','?')} |",
        f"| Duplicate keys | {stock.get('summary',{}).get('total_duplicates_on_key','?')} |",
        f"",
        f"### Partition Detail",
        f"",
        f"| Partition | Rows | Min Date | Max Date | Dups |",
        f"|-----------|------|----------|----------|------|",
    ]
    for p in stock.get("partitions", []):
        lines.append(
            f"| {p['partition']} | {p['rows']:,} | {p.get('min_date','')} | {p.get('max_date','')} | {p['duplicates_on_key']} |"
        )

    if stock.get("errors"):
        lines += ["", "### Errors", ""]
        for e in stock["errors"]:
            lines.append(f"- {e}")

    lines += [
        f"",
        f"## fact_kospi",
        f"",
        f"| 항목 | 값 |",
        f"|------|-----|",
        f"| Status | {kospi.get('status','?')} |",
        f"| Rows | {kospi.get('rows','?')} |",
        f"| Date range | {kospi.get('min_date','?')} ~ {kospi.get('max_date','?')} |",
        f"| Duplicate dates | {kospi.get('duplicate_dates','?')} |",
        f"",
        f"## dim_theme",
        f"",
        f"| 항목 | 값 |",
        f"|------|-----|",
        f"| Status | {theme.get('status','?')} |",
        f"| Rows | {theme.get('rows','?')} |",
        f"| Duplicate codes | {theme.get('duplicate_codes','?')} |",
        f"| Unique theme1 | {theme.get('unique_theme1','?')} |",
        f"",
        f"## Storage",
        f"",
        f"| 항목 | 값 |",
        f"|------|-----|",
        f"| Total Parquet | {sizes['total_parquet_mb']} MB |",
        f"| Manifest | {sizes['manifest_kb']} KB |",
        f"",
        f"## Overall",
        f"",
    ]
    overall = "PASS" if all(
        r.get("status") in ("PASS", "WARN") for r in [stock, kospi, theme]
    ) else "FAIL"
    lines.append(f"**Status: {overall}**")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Validate Parquet data lake (E1)")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--report-format", choices=["md", "json", "both"], default="both")
    args = parser.parse_args()

    parquet_root = Path(args.output_dir)
    print(f"Validating: {parquet_root}")

    results = {
        "fact_daily_stock": validate_fact_stock(parquet_root),
        "fact_kospi": validate_fact_kospi(parquet_root),
        "dim_theme": validate_dim_theme(parquet_root),
        "sizes": compute_parquet_size(parquet_root),
        "validated_at": datetime.utcnow().isoformat(),
    }

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    if args.report_format in ("json", "both"):
        json_path = DOCS_DIR / "e1_validation.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"JSON report: {json_path}")

    if args.report_format in ("md", "both"):
        md_path = DOCS_DIR / "e1_report.md"
        md_content = generate_md_report(results, parquet_root)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"MD report: {md_path}")

    stock_status = results["fact_daily_stock"].get("status", "FAIL")
    print(f"\nOverall status: {'PASS' if stock_status == 'PASS' else 'FAIL'}")

    if results["fact_daily_stock"].get("errors"):
        print("\nErrors:")
        for e in results["fact_daily_stock"]["errors"]:
            print(f"  - {e}")

    summary = results["fact_daily_stock"].get("summary", {})
    if summary:
        print(f"\nfact_daily_stock: {summary.get('total_rows',0):,} rows | "
              f"{summary.get('total_partitions',0)} months | "
              f"{summary.get('min_date','?')} ~ {summary.get('max_date','?')} | "
              f"{results['sizes']['total_parquet_mb']} MB total")

    sys.exit(0 if stock_status == "PASS" else 1)


if __name__ == "__main__":
    main()

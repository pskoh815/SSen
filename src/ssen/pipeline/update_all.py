"""
E7: 원클릭 업데이트 파이프라인

Usage:
    python -m ssen.pipeline.update_all [OPTIONS]

Options:
    --incoming-dir PATH    (기본: data/incoming)
    --overlap POLICY       rebuild|new_only (기본: rebuild)
    --dry-run              영향 파티션/날짜만 출력, 실제 변경 없음
    --skip-e3              derived 재계산 생략 (빠른 데이터 갱신 시)
    --no-archive           처리 후 archive 이동 생략

실행 순서 (idempotent):
    [1] incoming/*.xlsx 스캔 + 날짜 범위 분석
    [2] E1: Excel → Parquet (월 파티션 재생성)
    [3] E2: Parquet → Postgres (증분 적재)
    [4] E3: derived_* 재계산 (recalc_start = new_min - LOOKBACK_DAYS)
    [5] 캐시 무효화
    [6] Archive 이동
    [7] pipeline_runs 기록
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ssen.pipeline.state import (
    get_manifest_max_date, get_manifest_months, get_db_watermark,
    get_pipeline_last_success, inspect_incoming_file,
    compute_affected_months,
)

LOOKBACK_DAYS = 60   # E3 재계산 버퍼 (캘린더 일)


# ── DB 기록 ──────────────────────────────────────────────────────────────────

def _start_pipeline_run(conn, input_files: list[str], prev_max: Optional[date],
                        new_min: Optional[date], new_max: Optional[date],
                        months: list[str], dry_run: bool) -> int:
    from ssen.db.connection import get_cur
    with get_cur(conn) as cur:
        cur.execute("""
            INSERT INTO pipeline_runs
              (input_files, prev_max_date, new_min_date, new_max_date,
               affected_months, dry_run, status)
            VALUES (%s,%s,%s,%s,%s,%s,'running')
            RETURNING run_id
        """, (input_files, prev_max, new_min, new_max, months, dry_run))
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def _finish_pipeline_run(conn, run_id: int, steps: list[str],
                         dataset_version: str, status: str,
                         error_msg: Optional[str] = None) -> None:
    from ssen.db.connection import get_cur
    with get_cur(conn) as cur:
        cur.execute("""
            UPDATE pipeline_runs
            SET finished_at     = now(),
                steps_done      = %s,
                dataset_version = %s,
                status          = %s,
                error_msg       = %s
            WHERE run_id = %s
        """, (steps, dataset_version, status, error_msg, run_id))
    conn.commit()


# ── 파이프라인 ────────────────────────────────────────────────────────────────

def run(
    incoming_dir: Path,
    parquet_dir: Path,
    overlap: str = "rebuild",
    dry_run: bool = False,
    skip_e3: bool = False,
    no_archive: bool = False,
) -> dict:
    t_start = time.time()
    now_utc = datetime.now(timezone.utc)

    print(f"\n{'='*60}")
    print(f"E7 원클릭 업데이트 파이프라인")
    print(f"{'='*60}")
    print(f"  incoming:  {incoming_dir}")
    print(f"  overlap:   {overlap}")
    print(f"  dry-run:   {dry_run}")
    print(f"  시작:      {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")

    # ── [1] incoming 스캔 ─────────────────────────────────────────────────────
    xlsx_files = sorted(incoming_dir.glob("*.xlsx"))
    if not xlsx_files:
        print(f"\n  incoming 파일 없음: {incoming_dir}")
        print("  → data/incoming/ 에 .xlsx 파일을 넣고 다시 실행하세요.")
        return {"status": "skipped", "reason": "no incoming files"}

    print(f"\n[1] incoming 파일 스캔: {len(xlsx_files)}개")
    file_infos = []
    for f in xlsx_files:
        info = inspect_incoming_file(f)
        file_infos.append(info)
        print(f"  {info['file']}: {info['rows']:,}행 | {info['min_date']} ~ {info['max_date']}")

    # 전체 날짜 범위
    all_mins = [date.fromisoformat(i["min_date"]) for i in file_infos if i["min_date"]]
    all_maxs = [date.fromisoformat(i["max_date"]) for i in file_infos if i["max_date"]]
    new_min = min(all_mins) if all_mins else None
    new_max = max(all_maxs) if all_maxs else None
    affected_months = compute_affected_months(new_min, new_max) if new_min and new_max else []

    # 현재 watermark
    prev_max = get_manifest_max_date()
    print(f"\n  현재 watermark (manifest max_date): {prev_max}")
    print(f"  신규 데이터 범위: {new_min} ~ {new_max}")
    print(f"  영향 파티션: {len(affected_months)}개월 ({affected_months[:3]}{'...' if len(affected_months)>3 else ''})")

    is_new_data = new_max and (prev_max is None or new_max > prev_max)
    print(f"  신규 데이터 여부: {'YES ✓' if is_new_data else 'NO (동일 또는 이전 데이터, idempotent 재처리)'}")

    if dry_run:
        print(f"\n{'='*60}")
        print("DRY-RUN 결과 (실제 변경 없음)")
        print(f"{'='*60}")
        print(f"  처리 대상 파일:  {[f['file'] for f in file_infos]}")
        print(f"  예상 날짜 범위:  {new_min} ~ {new_max}")
        print(f"  영향 파티션:     {affected_months}")
        print(f"  E3 재계산 기준:  {new_min - timedelta(days=LOOKBACK_DAYS) if new_min else 'N/A'}")
        print(f"  캐시 무효화:     leaders/*, trades/*")
        print(f"  archive 이동:    data/archive/{now_utc.strftime('%Y%m%dT%H%M%S')}/")
        return {
            "status": "dry_run",
            "input_files": [f["file"] for f in file_infos],
            "new_min": str(new_min),
            "new_max": str(new_max),
            "affected_months": affected_months,
        }

    # ── 실제 파이프라인 실행 ──────────────────────────────────────────────────
    from ssen.db.connection import get_conn

    steps_done = []
    run_id = None

    with get_conn() as conn:
        run_id = _start_pipeline_run(
            conn,
            [f["file"] for f in file_infos],
            prev_max, new_min, new_max,
            affected_months, dry_run=False,
        )
    print(f"\n  pipeline_run_id: {run_id}")

    try:
        # ── [2] E1: Excel → Parquet ───────────────────────────────────────────
        print(f"\n[2] E1: Excel → Parquet 변환...")
        from ssen.etl.convert_excel_to_parquet import convert_file
        for xlsx in xlsx_files:
            convert_file(xlsx, parquet_dir, overlap_policy=overlap)
        steps_done.append("e1_convert")
        print(f"  E1 완료")

        # ── [3] E2: Parquet → Postgres ────────────────────────────────────────
        print(f"\n[3] E2: Parquet → Postgres 증분 적재...")
        from ssen.db.load_parquet_to_postgres import load
        e2_result = load(
            parquet_dir=parquet_dir,
            months=affected_months if overlap == "new_only" else None,
            overlap_policy=overlap,
        )
        steps_done.append("e2_load")
        print(f"  E2 완료: {e2_result.get('total_rows', 0):,}행")

        # ── [4] E3: derived_* 재계산 ──────────────────────────────────────────
        if not skip_e3:
            print(f"\n[4] E3: 파생 테이블 재계산 (lookback={LOOKBACK_DAYS}일)...")
            recalc_start = (new_min - timedelta(days=LOOKBACK_DAYS)) if new_min else None
            from ssen.strategy.backtest import run as backtest_run
            from ssen.strategy.rules import DEFAULT_PARAMS
            e3_result = backtest_run(
                parquet_dir=parquet_dir,
                params=DEFAULT_PARAMS,
                start=recalc_start,
                end=new_max,
            )
            steps_done.append("e3_backtest")
            print(f"  E3 완료: regimes={e3_result.get('n_regimes',0)}, trades={e3_result.get('n_trades',0)}")
        else:
            print(f"\n[4] E3: 생략 (--skip-e3)")

        # ── [5] 캐시 무효화 ───────────────────────────────────────────────────
        print(f"\n[5] 캐시 무효화...")
        try:
            from ssen.api import cache as _cache
            n_leaders = _cache.invalidate_prefix("leaders")
            n_trades  = _cache.invalidate_prefix("trades")
            n_meta    = _cache.invalidate_prefix("meta")
            print(f"  leaders={n_leaders}, trades={n_trades}, meta={n_meta} 캐시 항목 삭제")
            steps_done.append("cache_invalidate")
        except Exception as e:
            print(f"  캐시 무효화 스킵 (API 미실행 상태): {e}")

        # ── [6] Archive 이동 ──────────────────────────────────────────────────
        if not no_archive:
            archive_dir = ROOT / "data" / "archive" / now_utc.strftime("%Y%m%dT%H%M%S")
            archive_dir.mkdir(parents=True, exist_ok=True)
            for xlsx in xlsx_files:
                dest = archive_dir / xlsx.name
                shutil.move(str(xlsx), str(dest))
                print(f"\n[6] 아카이브: {xlsx.name} → {archive_dir.name}/")
            steps_done.append("archive")
        else:
            print(f"\n[6] 아카이브: 생략 (--no-archive)")

        # ── [7] 완료 기록 ─────────────────────────────────────────────────────
        new_dataset_version = str(new_max) if new_max else "unknown"
        with get_conn() as conn:
            _finish_pipeline_run(conn, run_id, steps_done, new_dataset_version, "done")

        elapsed = time.time() - t_start
        print(f"\n{'='*60}")
        print(f"E7 파이프라인 완료!")
        print(f"  run_id:          {run_id}")
        print(f"  steps:           {' → '.join(steps_done)}")
        print(f"  prev max_date:   {prev_max}")
        print(f"  new max_date:    {new_max}")
        print(f"  dataset_version: {new_dataset_version}")
        print(f"  총 소요 시간:    {elapsed:.1f}초")
        print(f"{'='*60}")

        return {
            "status": "done",
            "run_id": run_id,
            "prev_max_date": str(prev_max),
            "new_max_date": str(new_max),
            "dataset_version": new_dataset_version,
            "steps": steps_done,
            "elapsed_sec": elapsed,
        }

    except Exception as e:
        error_msg = str(e)
        print(f"\n[ERROR] 파이프라인 실패: {error_msg}")
        print("  재실행 시 이전 성공 단계는 idempotent하게 재처리됩니다.")
        if run_id:
            try:
                with get_conn() as conn:
                    _finish_pipeline_run(conn, run_id, steps_done, "", "failed", error_msg)
            except Exception:
                pass
        raise


def main():
    parser = argparse.ArgumentParser(description="E7 원클릭 업데이트 파이프라인")
    parser.add_argument("--incoming-dir", default=str(ROOT / "data" / "incoming"))
    parser.add_argument("--parquet-dir",  default=str(ROOT / "data" / "parquet"))
    parser.add_argument("--overlap", choices=["rebuild","new_only"], default="rebuild")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--skip-e3",    action="store_true")
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args()

    result = run(
        incoming_dir=Path(args.incoming_dir),
        parquet_dir=Path(args.parquet_dir),
        overlap=args.overlap,
        dry_run=args.dry_run,
        skip_e3=args.skip_e3,
        no_archive=args.no_archive,
    )
    sys.exit(0 if result.get("status") in ("done", "dry_run", "skipped") else 1)


if __name__ == "__main__":
    main()

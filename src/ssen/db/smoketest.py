"""
E2 smoketest: 대표 쿼리 5개 실행 + 시간 측정
Usage: python -m ssen.db.smoketest
"""
import time
import sys
from .connection import get_conn, get_cur

QUERIES = [
    (
        "Q1. 특정 기간 상위 10 종목 (date 범위 필터 + rank 정렬)",
        """
        SELECT date, rank, code, name, change_pct, strength
        FROM   fact_daily_stock
        WHERE  date BETWEEN '2025-01-01' AND '2025-03-31'
          AND  rank <= 10
        ORDER  BY date, rank
        LIMIT  30
        """,
    ),
    (
        "Q2. 테마별 누적 거래대금 TOP 10 (기간 집계)",
        """
        SELECT   theme1, SUM(theme1_amount) AS total_amount, COUNT(*) AS cnt
        FROM     fact_daily_stock
        WHERE    date BETWEEN '2025-01-01' AND '2025-12-31'
          AND    theme1 IS NOT NULL
        GROUP BY theme1
        ORDER BY total_amount DESC NULLS LAST
        LIMIT    10
        """,
    ),
    (
        "Q3. 특정 종목 일별 등락률 추이 (종목 조회)",
        """
        SELECT date, code, name, change_pct, strength
        FROM   fact_daily_stock
        WHERE  date BETWEEN '2024-01-01' AND '2026-05-29'
          AND  code = '005930'
        ORDER  BY date
        """,
    ),
    (
        "Q4. 코스피 기간 시세 + 상승하락비율 JOIN",
        """
        SELECT k.date, k.close, k.change_rate,
               a.up_count, a.down_count, a.adr
        FROM   fact_kospi_index k
        JOIN   fact_adr a ON k.date = a.date AND a.index_name = 'KOSPI'
        WHERE  k.date BETWEEN '2025-01-01' AND '2025-06-30'
        ORDER  BY k.date
        LIMIT  20
        """,
    ),
    (
        "Q5. 강세(strength) 종목 수 월별 집계",
        """
        SELECT   DATE_TRUNC('month', date)::date AS month,
                 strength,
                 COUNT(DISTINCT code) AS stock_count
        FROM     fact_daily_stock
        WHERE    date BETWEEN '2024-01-01' AND '2026-05-29'
          AND    strength IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
    ),
]


def run_smoketest() -> bool:
    print("=" * 60)
    print("E2 Smoketest - 대표 쿼리 5개")
    print("=" * 60)
    all_pass = True

    with get_conn() as conn:
        for label, sql in QUERIES:
            print(f"\n{label}")
            t0 = time.time()
            try:
                with get_cur(conn) as cur:
                    cur.execute(sql)
                    rows = cur.fetchall()
                elapsed = (time.time() - t0) * 1000
                print(f"  => {len(rows)}행 | {elapsed:.1f}ms")
                if rows:
                    # 첫 행 미리보기
                    cols = [d[0] for d in cur.description]
                    preview = dict(zip(cols, rows[0]))
                    print(f"  첫 행: {preview}")
                print(f"  [PASS]")
            except Exception as e:
                elapsed = (time.time() - t0) * 1000
                print(f"  [FAIL] {e} ({elapsed:.1f}ms)")
                all_pass = False

    print("\n" + "=" * 60)
    print(f"결과: {'PASS' if all_pass else 'FAIL'}")
    print("=" * 60)
    return all_pass


def main():
    ok = run_smoketest()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

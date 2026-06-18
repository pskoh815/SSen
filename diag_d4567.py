import sys, time, json
sys.stdout.reconfigure(encoding='utf-8')
from ssen.db.connection import get_conn, get_cur

QUERIES = {
    "Q1 (fact_daily_stock, rank<=20)": """
        SELECT date, code, rank, close_price
        FROM fact_daily_stock
        WHERE date BETWEEN '2023-01-01' AND '2023-12-31' AND rank <= 20
        ORDER BY date, rank
    """,
    "Q2 (derived_leader_regime)": """
        SELECT regime_id, theme1, leader_code, start_date, end_date
        FROM derived_leader_regime
        WHERE start_date <= '2023-12-31' AND end_date >= '2023-01-01'
        ORDER BY start_date
    """,
    "Q3 (derived_trades)": """
        SELECT trade_id, code, signal_date, entry_date, exit_date, net_pnl_pct
        FROM derived_trades
        WHERE entry_date BETWEEN '2023-01-01' AND '2023-12-31'
        ORDER BY entry_date
    """,
}

print("=" * 70)
print("D4: DB 쿼리 속도 (EXPLAIN ANALYZE)")
print("=" * 70)
with get_conn() as conn:
    with get_cur(conn) as cur:
        for label, q in QUERIES.items():
            print(f"\n--- {label} ---")
            t0 = time.perf_counter()
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + q)
            plan_lines = [r[0] for r in cur.fetchall()]
            elapsed = (time.perf_counter() - t0) * 1000
            plan_text = "\n".join(plan_lines)
            print(plan_text)
            scan_kind = "Seq Scan" if "Seq Scan" in plan_text else (
                        "Index Scan" if "Index Scan" in plan_text or "Index Only Scan" in plan_text else
                        "Bitmap/Other")
            pruning = "예" if ("Partitions Removed" in plan_text or "never executed" in plan_text) else "해당없음/미확인"
            idx_used = [l.strip() for l in plan_lines if "Index" in l and "Scan" in l]
            print(f"\n[요약] EXPLAIN 실행시간(클라이언트 측 RTT 포함): {elapsed:.1f} ms")
            print(f"[요약] 스캔 방식: {scan_kind} | 인덱스 사용: {idx_used if idx_used else '없음'} | 파티션 프루닝 표시: {pruning}")

print()
print("=" * 70)
print("D5: derived 테이블 상태")
print("=" * 70)
with get_conn() as conn:
    with get_cur(conn) as cur:
        for t in ["derived_theme_daily", "derived_leader_regime", "derived_trades"]:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            cnt = cur.fetchone()[0]
            date_col = "date" if t == "derived_theme_daily" else ("end_date" if t == "derived_leader_regime" else "exit_date")
            cur.execute(f"SELECT MAX({date_col}) FROM {t}")
            maxd = cur.fetchone()[0]
            print(f"  {t:24s} rows={cnt:>8,}   max({date_col})={maxd}")

        cur.execute("SELECT MAX(date) FROM fact_daily_stock")
        fact_max = cur.fetchone()[0]
        print(f"\n  fact_daily_stock max(date) = {fact_max}")

        cur.execute("SELECT MAX(end_date) FROM derived_leader_regime")
        regime_max = cur.fetchone()[0]
        cur.execute("SELECT MAX(exit_date) FROM derived_trades")
        trades_max = cur.fetchone()[0]
        if regime_max:
            cur.execute("SELECT (%s::date - %s::date)", (fact_max, regime_max))
            gap_regime = cur.fetchone()[0]
            print(f"  derived_leader_regime 지연: fact 대비 {gap_regime}일 뒤처짐 (max_end_date={regime_max})")
        if trades_max:
            cur.execute("SELECT (%s::date - %s::date)", (fact_max, trades_max))
            gap_trades = cur.fetchone()[0]
            print(f"  derived_trades 지연:        fact 대비 {gap_trades}일 뒤처짐 (max_exit_date={trades_max})")

print()
print("=" * 70)
print("D6: etl_runs / pipeline_runs 기록 분석")
print("=" * 70)
with get_conn() as conn:
    with get_cur(conn) as cur:
        for t in ["etl_runs", "pipeline_runs"]:
            cur.execute("SELECT to_regclass(%s)", (t,))
            exists = cur.fetchone()[0]
            print(f"\n--- {t} (테이블 존재: {'예' if exists else '아니오'}) ---")
            if not exists:
                continue
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            print(f"  총 레코드 수: {cur.fetchone()[0]}")
            if t == "etl_runs":
                cur.execute("""
                    SELECT run_id, started_at, finished_at,
                           array_length(partitions, 1) AS n_partitions,
                           status,
                           EXTRACT(EPOCH FROM (finished_at - started_at)) AS dur_sec
                    FROM etl_runs ORDER BY run_id DESC LIMIT 10
                """)
            else:
                cur.execute("""
                    SELECT run_id, started_at, finished_at,
                           array_length(affected_months, 1) AS n_partitions,
                           status,
                           EXTRACT(EPOCH FROM (finished_at - started_at)) AS dur_sec,
                           steps_done, error_msg
                    FROM pipeline_runs ORDER BY run_id DESC LIMIT 10
                """)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            for r in rows:
                d = dict(zip(cols, r))
                print(f"  run={d.get('run_id')}  start={d.get('started_at')}  "
                      f"end={d.get('finished_at')}  partitions={d.get('n_partitions')}  "
                      f"status={d.get('status')}  소요(s)={d.get('dur_sec')}")
                if d.get('status') not in ('done', 'success', None) :
                    print(f"      └ 비정상 상태! error_msg={d.get('error_msg')}, steps_done={d.get('steps_done')}")
            durs = [r[-1] if t=='etl_runs' else r[5] for r in rows if (r[-1] if t=='etl_runs' else r[5]) is not None]
            if durs:
                worst = max(zip(rows, durs), key=lambda x: x[1])
                print(f"  >> 가장 오래 걸린 run: run_id={worst[0][0]}  소요={worst[1]:.1f}s")

print()
print("=" * 70)
print("D7: 캐시 상태 (Redis)")
print("=" * 70)
try:
    import redis
    r = redis.from_url("redis://localhost:6379", socket_connect_timeout=2, socket_timeout=2)
    r.ping()
    info = r.info()
    dbsize = r.dbsize()
    keys_sample = r.keys("ssen:*")
    print(f"  Redis 연결: 성공")
    print(f"  적재된 키 수(dbsize): {dbsize}")
    print(f"  ssen:* 패턴 키 수: {len(keys_sample)}")
    print(f"  사용 메모리: {info.get('used_memory_human')}  (peak: {info.get('used_memory_peak_human')})")
    print(f"  maxmemory: {info.get('maxmemory_human', info.get('maxmemory'))}")
    kh = info.get('keyspace_hits', 0)
    km = info.get('keyspace_misses', 0)
    total = kh + km
    print(f"  keyspace_hits={kh}  keyspace_misses={km}  히트율={ (kh/total*100) if total else 0:.1f}%")
except Exception as e:
    print(f"  Redis 연결 실패: {e}")
    print(f"  => API 프로세스는 ssen.api.cache의 TTLCache(in-memory) fallback으로 동작 중일 가능성")
    print(f"     (in-memory 캐시는 API 프로세스가 떠 있을 때만 통계 조회 가능 — 별도 프로세스에서는 조회 불가)")

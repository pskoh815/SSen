import sys, time
sys.stdout.reconfigure(encoding='utf-8')
import pyarrow.dataset as ds
import pyarrow.compute as pc
import datetime as dt

COLS = ['date', 'code', 'rank', 'volume_sum']

def load(start, end):
    t0 = time.perf_counter()
    dataset = ds.dataset('data/parquet/fact_daily_stock', format='parquet', partitioning='hive')
    filt = (pc.field('date') >= dt.date.fromisoformat(start)) & (pc.field('date') <= dt.date.fromisoformat(end))
    table = dataset.to_table(columns=COLS, filter=filt)
    elapsed = time.perf_counter() - t0
    return elapsed, table.num_rows

def load_with_fragment_count(start, end):
    dataset = ds.dataset('data/parquet/fact_daily_stock', format='parquet', partitioning='hive')
    filt = (pc.field('date') >= dt.date.fromisoformat(start)) & (pc.field('date') <= dt.date.fromisoformat(end))
    frags = list(dataset.get_fragments(filter=filt))
    return len(frags)

print("=== D3: Parquet I/O 속도 측정 (pyarrow.dataset, hive partitioning) ===")
print(f"컬럼: {COLS}\n")

# warm-up (파일시스템 캐시 영향 제거 위해 1회 실행 후 측정)
_ = load('2023-01-01', '2023-12-31')

n = 3
print("-- 1년치 (2023-01-01 ~ 2023-12-31) --")
times_1y = []
for i in range(n):
    el, rows = load('2023-01-01', '2023-12-31')
    times_1y.append(el)
    print(f"  run {i+1}: {el*1000:.1f} ms, rows={rows}")
frags_1y = load_with_fragment_count('2023-01-01', '2023-12-31')
print(f"  스캔된 파티션(fragment) 수: {frags_1y}")
avg_1y = sum(times_1y) / n

print("\n-- 전체 6년 (2020-01-01 ~ 2026-05-31) --")
times_6y = []
for i in range(n):
    el, rows = load('2020-01-01', '2026-05-31')
    times_6y.append(el)
    print(f"  run {i+1}: {el*1000:.1f} ms, rows={rows}")
frags_6y = load_with_fragment_count('2020-01-01', '2026-05-31')
print(f"  스캔된 파티션(fragment) 수: {frags_6y}")
avg_6y = sum(times_6y) / n

ratio = avg_6y / avg_1y
print(f"\n=== 결과 요약 ===")
print(f"1년치 평균:  {avg_1y*1000:.1f} ms  (fragment {frags_1y}개)")
print(f"6년치 평균:  {avg_6y*1000:.1f} ms  (fragment {frags_6y}개)")
print(f"비율(6년/1년): {ratio:.2f}배")
print(f"전체 fragment(파티션) 수 / 1년 fragment 수 = {frags_6y/frags_1y:.2f}배")
if ratio > 6:
    print("=> 6배를 초과 — 파티션 프루닝이 기대만큼 작동하지 않을 가능성")
else:
    print("=> 6배 이하 — 데이터 규모에 비례, 프루닝 정상 작동 추정")

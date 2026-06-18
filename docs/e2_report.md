# E2 Report - Postgres 적재 + 쿼리 성능

Generated: 2026-06-04

## 적재 결과

| 항목 | 값 |
|------|-----|
| 소스 | SSen분析.xlsx (C:\MyPy) |
| fact_daily_stock | **157,200행**, 77개 파티션 |
| fact_kospi_index | 1,573행, 77개 파티션 |
| fact_adr | 3,050행, 77개 파티션 |
| dim_theme | 3,042행 |
| dim_stock | ~1,000+ 종목 (upsert) |
| map_stock_theme | 3,402행 |
| 날짜 범위 | 2020-01-02 ~ 2026-05-29 |
| 총 적재 시간 | **13.6초** (전체 77개월, COPY bulk) |
| run_id | 6 |

## Smoketest - 쿼리 5개 실행 결과

| # | 쿼리 | 결과 행수 | 실행 시간 | 상태 |
|---|------|----------|----------|------|
| Q1 | 기간 필터 + rank≤10 상위 종목 | 30 | **8.4ms** | PASS |
| Q2 | 테마별 누적 거래대금 TOP 10 집계 | 10 | **30.2ms** | PASS |
| Q3 | 삼성전자(005930) 전체 기간 일별 조회 | 585 | **37.0ms** | PASS |
| Q4 | 코스피 시세 + ADR JOIN | 20 | **143.3ms** | PASS |
| Q5 | strength별 종목수 월별 집계 | 199 | **111.4ms** | PASS |

## 환경

| 항목 | 값 |
|------|-----|
| PostgreSQL | 17.10 / Windows x64 |
| 인증 | trust (127.0.0.1) + scram-sha-256 (::1) |
| 파티셔닝 | RANGE(date), 월 단위, 77개 파티션/테이블 |
| 주요 인덱스 | (date,rank), (date,code), (date,theme1), BRIN(date) |

## etl_runs 이력

```sql
SELECT run_id, started_at, min_date, max_date, total_rows, status
FROM etl_runs ORDER BY run_id;
```

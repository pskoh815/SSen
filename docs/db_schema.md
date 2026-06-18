# DB Schema (E2)

## 접속 정보

| 항목 | 값 |
|------|-----|
| Host | localhost:5432 |
| Database | ssen |
| User | ssen / ssen |
| PostgreSQL | 17.x |
| 환경변수 오버라이드 | `SSEN_DB_URL` |

## 스타 스키마 개요

```
dim_stock ──┐
dim_theme ──┤──→ map_stock_theme
            │
            ├──→ fact_daily_stock (PARTITION BY RANGE(date), 월 단위)
            │
fact_kospi_index (PARTITION BY RANGE(date), 월 단위)
fact_adr         (PARTITION BY RANGE(date), 월 단위)
etl_runs         (파이프라인 실행 이력)
```

## 테이블 상세

### fact_daily_stock (주 사실 테이블)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| date | DATE | 거래일 (파티션 키) |
| rank | SMALLINT | 일별 거래대금 순위 |
| code | CHAR(6) | 종목코드 (PK 일부) |
| name | TEXT | 종목명 |
| market | TEXT | KOSPI/KOSDAQ |
| base_price | BIGINT | 시작일 기준가 |
| close_price | BIGINT | 종료일 종가 |
| change | BIGINT | 대비 |
| change_pct | DOUBLE | 등락률 |
| volume_sum | BIGINT | 거래량 합계 |
| amount_sum | BIGINT | 거래대금 합계 |
| mktcap | BIGINT | 시가총액 |
| contrib_score | DOUBLE | 기여점수 |
| theme1 | TEXT | 1차 테마 |
| theme2 | TEXT | 2차 테마 |
| strength | TEXT | 강약 판정 |

**PK**: (date, code, rank)  
**파티션**: `fact_daily_stock_YYYYMM` (월 단위, RANGE)

### fact_kospi_index

**PK**: (date) | **파티션**: 월 단위

### fact_adr (상승하락비율)

**PK**: (date, index_name) | **파티션**: 월 단위

### dim_stock

**PK**: code — fact_daily_stock에서 upsert

### dim_theme

**PK**: code — Excel 테마 시트에서 full reload

### map_stock_theme

| 컬럼 | 설명 |
|------|------|
| code | 종목코드 |
| theme_type | 1=1차테마, 2=2차테마 |
| theme_name | 테마명 |

### etl_runs (파이프라인 이력)

| 컬럼 | 설명 |
|------|------|
| run_id | 자동증가 PK |
| started_at | 시작 시각 |
| finished_at | 완료 시각 |
| input_files | 입력 파일 목록 |
| min_date | 적재 최소 날짜 |
| max_date | 적재 최대 날짜 (= dataset_version) |
| dataset_version | max_date 기반 버전 식별자 |
| partitions | 처리된 월 파티션 목록 |
| total_rows | 적재 총 행수 |
| status | running / done / failed |

## 인덱스 전략

| 인덱스 | 대상 | 목적 |
|--------|------|------|
| idx_fds_date_rank | (date, rank) | 일별 상위 종목 조회 (주요) |
| idx_fds_date_code | (date, code) | 종목별 기간 조회 |
| idx_fds_date_theme1 | (date, theme1) | 테마별 필터 |
| idx_fds_strength | (date, strength) | 강세/약세 필터 |
| idx_kospi_date_brin | BRIN(date) | 날짜 범위 스캔 |
| idx_adr_date | (date, index_name) | ADR 조회 |
| idx_dim_stock_name | GIN(tsvector) | 종목명 텍스트 검색 |

## 파티션 관리

```sql
-- 파티션 목록 조회
SELECT inhrelid::regclass AS partition, pg_get_expr(c.relpartbound, c.oid)
FROM   pg_inherits
JOIN   pg_class c ON inhrelid = c.oid
WHERE  inhparent = 'fact_daily_stock'::regclass
ORDER  BY 1;
```

## 증분 적재 정책

| 정책 | CLI 옵션 | 동작 |
|------|---------|------|
| rebuild (기본) | `--overlap rebuild` | 전체 월 재적재 (TRUNCATE+COPY) |
| new_only | `--overlap new_only` | etl_runs 미기록 월만 적재 |
| 특정 월 | `--months 202401 202402` | 지정 월만 적재 |

**Idempotent 보장**: 각 월 파티션은 `TRUNCATE → COPY`로 처리. 중복 실행 안전.

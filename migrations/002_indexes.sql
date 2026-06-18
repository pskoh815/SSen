-- Migration 002: 인덱스 전략
-- fact_daily_stock 파티션 테이블은 각 파티션별 로컬 인덱스 사용
-- (PostgreSQL 파티션 테이블에서 글로벌 인덱스는 제한적)

-- date+rank: 상위 종목 조회 (가장 빈번한 쿼리)
CREATE INDEX IF NOT EXISTS idx_fds_date_rank
    ON fact_daily_stock (date, rank);

-- date+code: 특정 종목 기간 조회
CREATE INDEX IF NOT EXISTS idx_fds_date_code
    ON fact_daily_stock (date, code);

-- theme1 필터링 (테마별 주도주 조회)
CREATE INDEX IF NOT EXISTS idx_fds_date_theme1
    ON fact_daily_stock (date, theme1)
    WHERE theme1 IS NOT NULL;

-- strength 필터링
CREATE INDEX IF NOT EXISTS idx_fds_strength
    ON fact_daily_stock (date, strength)
    WHERE strength IS NOT NULL;

-- kospi: date 범위 (BRIN이 효율적 - 날짜 순 정렬 보장)
CREATE INDEX IF NOT EXISTS idx_kospi_date_brin
    ON fact_kospi_index USING BRIN (date);

-- adr: date + index_name
CREATE INDEX IF NOT EXISTS idx_adr_date
    ON fact_adr (date, index_name);

-- dim_stock 검색
CREATE INDEX IF NOT EXISTS idx_dim_stock_name
    ON dim_stock USING gin(to_tsvector('simple', name));

-- map_stock_theme: 테마명 검색
CREATE INDEX IF NOT EXISTS idx_map_theme_name
    ON map_stock_theme (theme_name, theme_type);

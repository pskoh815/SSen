-- Migration 001: 초기 스키마 생성
-- star schema: dim_* + fact_* (date-partitioned)

-- ── etl_runs: 파이프라인 실행 이력 ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS etl_runs (
    run_id          SERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    input_files     TEXT[],
    min_date        DATE,
    max_date        DATE,
    dataset_version TEXT,           -- max_date::text
    partitions      TEXT[],         -- 적재된 yearmonth 목록
    total_rows      BIGINT,
    status          TEXT NOT NULL DEFAULT 'running'  -- running|done|failed
);

-- ── dim_stock: 종목 마스터 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dim_stock (
    code        CHAR(6)     PRIMARY KEY,
    name        TEXT        NOT NULL,
    market      TEXT,
    size_class  TEXT,
    shares      BIGINT,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- ── dim_theme: 테마 마스터 (3,042행, 비파티션) ────────────────────────────────
CREATE TABLE IF NOT EXISTS dim_theme (
    code        CHAR(6)     NOT NULL,
    name        TEXT        NOT NULL,
    theme1      TEXT,
    theme2      TEXT,
    shares      BIGINT,
    PRIMARY KEY (code)
);

-- ── map_stock_theme: 종목-테마 매핑 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS map_stock_theme (
    code        CHAR(6)     NOT NULL REFERENCES dim_theme(code) ON DELETE CASCADE,
    theme_type  SMALLINT    NOT NULL,   -- 1 or 2
    theme_name  TEXT        NOT NULL,
    PRIMARY KEY (code, theme_type)
);

-- ── fact_daily_stock: 주 사실 테이블 (월 파티션) ──────────────────────────────
CREATE TABLE IF NOT EXISTS fact_daily_stock (
    date                    DATE        NOT NULL,
    rank                    SMALLINT    NOT NULL,
    code                    CHAR(6)     NOT NULL,
    name                    TEXT,
    market                  TEXT,
    base_price              BIGINT,
    close_price             BIGINT,
    change                  BIGINT,
    change_pct              DOUBLE PRECISION,
    volume_sum              BIGINT,
    volume_avg              BIGINT,
    amount_sum              BIGINT,
    amount_avg              BIGINT,
    shares                  BIGINT,
    vs_kospi_pct            DOUBLE PRECISION,
    mktcap                  BIGINT,
    amount_vs_mktcap_pct    DOUBLE PRECISION,
    size_class              TEXT,
    contrib_score           DOUBLE PRECISION,
    contrib_rank            SMALLINT,
    theme1                  TEXT,
    theme1_rank             SMALLINT,
    theme1_amount           BIGINT,
    theme1_pct              DOUBLE PRECISION,
    theme2_rank             SMALLINT,
    theme2                  TEXT,
    theme2_amount           BIGINT,
    theme2_pct              DOUBLE PRECISION,
    strength                TEXT,
    PRIMARY KEY (date, code, rank)
) PARTITION BY RANGE (date);

-- ── fact_kospi_index: 코스피 일별 시세 (월 파티션) ───────────────────────────
CREATE TABLE IF NOT EXISTS fact_kospi_index (
    date        DATE            NOT NULL,
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      BIGINT,
    change_rate DOUBLE PRECISION,
    PRIMARY KEY (date)
) PARTITION BY RANGE (date);

-- ── fact_adr: 상승하락비율 (월 파티션) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS fact_adr (
    date        DATE    NOT NULL,
    index_name  TEXT    NOT NULL,
    up_count    INT,
    down_count  INT,
    flat_count  INT,
    adr         DOUBLE PRECISION,
    PRIMARY KEY (date, index_name)
) PARTITION BY RANGE (date);

-- ── schema_migrations: 마이그레이션 이력 ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ DEFAULT now()
);

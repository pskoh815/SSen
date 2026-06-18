-- Migration 003: 파생 테이블 (E3)

-- ── derived_theme_daily: 테마별 일별 집계 ────────────────────────────────────
CREATE TABLE IF NOT EXISTS derived_theme_daily (
    date             DATE    NOT NULL,
    theme1           TEXT    NOT NULL,
    theme_amount     BIGINT,               -- 테마 총 거래대금
    avg_change_pct   DOUBLE PRECISION,     -- 테마 내 종목 평균 등락률
    stock_count      SMALLINT,             -- 해당 날 랭킹 내 종목 수
    leader_code      CHAR(6),              -- 거래대금 최상위 종목 (global rank 최소)
    leader_name      TEXT,
    leader_rank      SMALLINT,             -- 리더의 전체 rank
    leader_close     BIGINT,               -- 리더 종가
    is_top_theme     BOOLEAN DEFAULT FALSE, -- 해당 날의 1위 테마 여부
    rule_version     TEXT    NOT NULL,
    dataset_version  TEXT    NOT NULL,
    PRIMARY KEY (date, theme1, rule_version, dataset_version)
);

CREATE INDEX IF NOT EXISTS idx_dtd_date_top
    ON derived_theme_daily (date, is_top_theme)
    WHERE is_top_theme = TRUE;

-- ── derived_leader_regime: 주도 테마 연속 구간 ────────────────────────────────
CREATE TABLE IF NOT EXISTS derived_leader_regime (
    regime_id         SERIAL  PRIMARY KEY,
    theme1            TEXT    NOT NULL,
    leader_code       CHAR(6),
    leader_name       TEXT,
    start_date        DATE    NOT NULL,
    end_date          DATE    NOT NULL,
    duration_days     INT     NOT NULL,   -- 거래일 기준 지속일
    avg_theme_amount  BIGINT,
    rule_version      TEXT    NOT NULL,
    dataset_version   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dlr_dates
    ON derived_leader_regime (start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_dlr_version
    ON derived_leader_regime (rule_version, dataset_version);

-- ── derived_trades: 진입/청산/갈아타기 로그 ─────────────────────────────────
CREATE TABLE IF NOT EXISTS derived_trades (
    trade_id         SERIAL  PRIMARY KEY,
    regime_id        INT,
    code             CHAR(6) NOT NULL,
    name             TEXT,
    theme1           TEXT,
    -- 룩어헤드 방지: 신호일(t)과 체결일(t+1) 분리
    signal_date      DATE    NOT NULL,    -- t일: 신호 발생 (regime 시작일)
    entry_date       DATE    NOT NULL,    -- t+1일: 진입 체결일
    entry_price      BIGINT,             -- t+1일 close_price
    exit_date        DATE,               -- 청산 체결일
    exit_price       BIGINT,             -- 청산일 close_price
    exit_reason      TEXT,               -- regime_end|stop_loss|take_profit|open
    pnl_pct          DOUBLE PRECISION,   -- 수익률 % (세전)
    fee_pct          DOUBLE PRECISION,   -- 수수료 % (왕복)
    net_pnl_pct      DOUBLE PRECISION,   -- 순 수익률 %
    hold_days        INT,                -- 보유 거래일 수
    rule_version     TEXT    NOT NULL,
    dataset_version  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dt_dates
    ON derived_trades (entry_date, exit_date);
CREATE INDEX IF NOT EXISTS idx_dt_version
    ON derived_trades (rule_version, dataset_version);
CREATE INDEX IF NOT EXISTS idx_dt_code
    ON derived_trades (code);

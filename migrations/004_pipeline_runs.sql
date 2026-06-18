-- Migration 004: 파이프라인 실행 이력 테이블 (E7)

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          SERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    input_files     TEXT[]       NOT NULL,
    prev_max_date   DATE,                    -- 파이프라인 실행 전 watermark
    new_min_date    DATE,                    -- 신규 데이터 최소 날짜
    new_max_date    DATE,                    -- 신규 데이터 최대 날짜 (= dataset_version)
    affected_months TEXT[],                  -- 영향받는 yearmonth 파티션 목록
    steps_done      TEXT[]       DEFAULT '{}',
    dataset_version TEXT,
    dry_run         BOOLEAN      DEFAULT FALSE,
    status          TEXT         NOT NULL DEFAULT 'running',  -- running|done|failed|skipped
    error_msg       TEXT
);

CREATE INDEX IF NOT EXISTS idx_pr_status  ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_pr_started ON pipeline_runs(started_at DESC);

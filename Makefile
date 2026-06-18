PYTHON     := python
SRC_DIR    := src
PARQUET_DIR:= data/parquet
INCOMING_DIR:= data/incoming
PGBIN      := C:/Program Files/PostgreSQL/17/bin
PSQL       := "$(PGBIN)/psql.exe" -U ssen -d ssen
PSQL_SU    := "$(PGBIN)/psql.exe" -U postgres

.PHONY: help \
        e1_convert e1_validate \
        db_up db_status e2_migrate e2_load e2_smoketest \
        update

# ── Help ────────────────────────────────────────────────────────────────────
help:
	@echo "SSen Dashboard - Make Targets"
	@echo ""
	@echo "  E1 (Excel -> Parquet)"
	@echo "    make e1_convert    Excel -> Parquet 월 파티션 변환"
	@echo "    make e1_validate   Parquet 품질 검증 + 리포트"
	@echo ""
	@echo "  E2 (Postgres)"
	@echo "    make db_up         PostgreSQL 서비스 확인/시작"
	@echo "    make e2_migrate    스키마/파티션 마이그레이션"
	@echo "    make e2_load       Parquet -> Postgres 전체 적재"
	@echo "    make e2_smoketest  쿼리 5개 검증"
	@echo ""
	@echo "  Pipeline"
	@echo "    make update        E1->E2 원클릭 전체 업데이트"
	@echo ""
	@echo "Options (env vars):"
	@echo "  OVERLAP=rebuild|new_only   파티션 정책 (기본: rebuild)"
	@echo "  MONTHS='202401 202402'     특정 월만 적재"
	@echo "  DRY_RUN=1                  파일/DB 쓰기 없이 미리 보기"
	@echo "  SSEN_DB_URL=postgresql://  DB 접속 URL 오버라이드"

# ── E1 ──────────────────────────────────────────────────────────────────────
e1_convert:
	@echo "=== E1: Excel -> Parquet 변환 ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.etl.convert_excel_to_parquet \
		--input-dir $(INCOMING_DIR) \
		--output-dir $(PARQUET_DIR) \
		--overlap $(if $(OVERLAP),$(OVERLAP),rebuild) \
		$(if $(filter 1,$(DRY_RUN)),--dry-run,)

e1_validate:
	@echo "=== E1: Parquet 검증 ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.etl.validate_parquet \
		--output-dir $(PARQUET_DIR) \
		--report-format both

# ── E2 ──────────────────────────────────────────────────────────────────────
db_up:
	@echo "=== PostgreSQL 서비스 확인 ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -c "\
import psycopg2, os; \
url = os.environ.get('SSEN_DB_URL','postgresql://ssen:ssen@localhost:5432/ssen'); \
conn = psycopg2.connect(url); \
print('DB connected:', conn.server_version); \
conn.close()"

e2_migrate:
	@echo "=== E2: DB 마이그레이션 ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.db.migrate

e2_load:
	@echo "=== E2: Parquet -> Postgres 적재 ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.db.load_parquet_to_postgres \
		--parquet-dir $(PARQUET_DIR) \
		--overlap $(if $(OVERLAP),$(OVERLAP),rebuild) \
		$(if $(MONTHS),--months $(MONTHS),) \
		$(if $(filter 1,$(DRY_RUN)),--dry-run,)

e2_smoketest:
	@echo "=== E2: Smoketest ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.db.smoketest

# ── E3 ──────────────────────────────────────────────────────────────────────
e3_backtest_default:
	@echo "=== E3: 파생 테이블 계산 (default 룰) ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.strategy.backtest \
		--parquet-dir $(PARQUET_DIR) \
		--rule default \
		$(if $(START_DATE),--start-date $(START_DATE),) \
		$(if $(END_DATE),--end-date $(END_DATE),) \
		$(if $(filter 1,$(DRY_RUN)),--dry-run,)

e3_backtest_conservative:
	@echo "=== E3: 파생 테이블 계산 (conservative 룰) ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.strategy.backtest \
		--parquet-dir $(PARQUET_DIR) \
		--rule conservative \
		$(if $(START_DATE),--start-date $(START_DATE),) \
		$(if $(END_DATE),--end-date $(END_DATE),)

# ── E4 ──────────────────────────────────────────────────────────────────────
api_up:
	@echo "=== E4: FastAPI 서버 시작 (http://localhost:8000/docs) ==="
	PYTHONPATH=$(SRC_DIR) uvicorn ssen.api.main:app --host 0.0.0.0 --port 8000 --reload

ui_up: api_up

# ── Market Data: OHLCV 수집 (data.go.kr) ────────────────────────────────────
collect_ohlcv:
	@echo "=== OHLCV 전체 수집 (2020~오늘, KOSPI+KOSDAQ) ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.market.collect_ohlcv \
		--start-date $(if $(START_DATE),$(START_DATE),20200102) \
		--end-date   $(if $(END_DATE),$(END_DATE),20260529) \
		--market     $(if $(MARKET),$(MARKET),ALL)

collect_ohlcv_test:
	@echo "=== OHLCV 테스트 수집 (최근 1개월) ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.market.collect_ohlcv \
		--start-date 20260501 --end-date 20260529 --market ALL

# ── E7: 원클릭 업데이트 ─────────────────────────────────────────────────────
update:
	@echo "=== E7: 원클릭 업데이트 파이프라인 ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.pipeline.update_all \
		--incoming-dir $(INCOMING_DIR) \
		--parquet-dir  $(PARQUET_DIR) \
		--overlap $(if $(OVERLAP),$(OVERLAP),rebuild)

update_dry:
	@echo "=== E7: 원클릭 업데이트 (Dry-run) ==="
	PYTHONPATH=$(SRC_DIR) $(PYTHON) -m ssen.pipeline.update_all \
		--incoming-dir $(INCOMING_DIR) \
		--parquet-dir  $(PARQUET_DIR) \
		--dry-run

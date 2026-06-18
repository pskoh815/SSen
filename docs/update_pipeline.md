# 원클릭 업데이트 파이프라인 (E7)

## 기본 사용법

```powershell
# 1. 새 Excel 파일을 incoming 폴더에 복사
Copy-Item "C:\MyPy\SSen분析.xlsx" ".\data\incoming\"

# 2. 한 명령으로 전체 업데이트
.\run.ps1 update
# 또는: make update

# Dry-run (파일 분석만, 실제 변경 없음)
.\run.ps1 update_dry
# 또는: make update_dry
```

## 폴더 규약

```
ssen-dashboard/
├── data/
│   ├── incoming/        ← 신규 Excel 파일을 여기에 넣기
│   │   └── SSen분析.xlsx
│   ├── parquet/         ← Parquet 데이터 레이크 (자동 관리)
│   └── archive/         ← 처리 완료 파일 자동 이동
│       └── 20260604T070000/
│           └── SSen분析.xlsx
```

## 파이프라인 실행 순서

```
incoming/*.xlsx
    │
    ▼ [1] 파일 스캔 + 날짜 범위 분석
    │
    ▼ [2] E1: Excel → Parquet 월 파티션 (rebuild)
    │
    ▼ [3] E2: Parquet → Postgres 증분 적재 (TRUNCATE+COPY, idempotent)
    │
    ▼ [4] E3: derived_* 재계산
    │         recalc_start = new_min_date - 60일 (lookback buffer)
    │
    ▼ [5] 캐시 무효화 (leaders/*, trades/*, meta/*)
    │
    ▼ [6] Archive 이동 (data/archive/<timestamp>/)
    │
    ▼ [7] pipeline_runs 기록
```

## Dry-run 출력 예시

```
DRY-RUN 결과 (실제 변경 없음)
  처리 대상 파일:  ['SSen분析.xlsx']
  예상 날짜 범위:  2020-01-02 ~ 2026-05-29
  영향 파티션:     ['202001', '202002', ..., '202605'] (77개)
  E3 재계산 기준:  2019-11-03 (60일 버퍼)
  캐시 무효화:     leaders/*, trades/*
  archive 이동:    data/archive/20260604T070000/
```

## 지원 파일 패턴

| 형태 | 예시 | 처리 방식 |
|------|------|---------|
| 기존 파일 업데이트 | `SSen분析.xlsx` (행 추가됨) | rebuild (전체 재처리) |
| 신규 날짜 파일 | `SSen분析_20260605.xlsx` | 병합 후 rebuild |
| 다수 파일 동시 투입 | 여러 .xlsx | 모두 스캔 후 날짜 범위 통합 |

## idempotent 보장

- **동일 파일 재처리**: 같은 날짜 범위라면 "NO (동일 데이터)" 판별 후 그래도 재처리 → 결과 동일
- **E1 Parquet**: 월 파티션 파일 덮어쓰기 (멱등)
- **E2 Postgres**: 파티션별 TRUNCATE+COPY (멱등)
- **E3 derived**: DELETE+INSERT (멱등)

## 실패 시 대응

| 단계 | 실패 원인 | 대응 |
|------|----------|------|
| E1 | Excel 파일 깨짐 | 파일 확인 후 재실행 |
| E2 | DB 연결 끊김 | PostgreSQL 서비스 확인 후 재실행 |
| E3 | Parquet 파일 없음 | E1 먼저 실행 후 재실행 |
| 전체 | 부분 실패 | `make update` 재실행 (idempotent) |

`pipeline_runs` 테이블에서 실패 단계 확인:
```sql
SELECT run_id, status, steps_done, error_msg
FROM pipeline_runs ORDER BY started_at DESC LIMIT 5;
```

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `SSEN_DB_URL` | `postgresql://ssen:ssen@127.0.0.1:5432/ssen` | DB 접속 |
| `SSEN_REDIS_URL` | `redis://localhost:6379` | Redis (없으면 TTLCache) |
| `OVERLAP` | `rebuild` | 파티션 정책 (make 변수) |

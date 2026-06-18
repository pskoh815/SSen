# SSen 대시보드 로드맵

## 초기 구축 절차

| 단계 | 에픽 | 내용 | 상태 |
|------|------|------|------|
| E1 | Excel → Parquet | xlsx 4시트를 월 파티션 Parquet으로 변환, 스키마 고정, 품질검증 | ✅ 완료 |
| E2 | Parquet → Postgres | 월 파티션 자동 생성, 증분 적재, etl_runs 테이블 | 예정 |
| E3 | 파생테이블 | derived_주도주/매수신호/매도신호/갈아타기 계산 | 예정 |
| E4 | REST API | FastAPI, /meta/health, /dashboard/query, 캐시 | 예정 |
| E5 | HTML 대시보드 | 기간 입력 → 주도주/매수/매도/수익률 출력 | 예정 |
| E6 | 캐시 & 스케일 | Redis 캐시, dataset_version 연동, 성능 튜닝 | 예정 |

---

## 반복 업데이트 절차 (E-UPDATE)

신규 Excel 파일이 들어오면 아래 순서로 전 레이어를 자동 최신화한다.

```
신규 xlsx 파일 → data/incoming/ 에 복사
       ↓
make update  (또는 python -m ssen.pipeline.update_all)
       ↓
[1] E1: Excel → Parquet 월 파티션 생성/갱신 + manifest 기록
[2] E2: Postgres 신규 월 파티션 자동 생성 + 증분 적재 (idempotent)
[3] E3: derived_* 신규 구간만 증분 재계산 (lookback buffer 포함)
[4] E4: API는 즉시 최신 derived_* 조회 (캐시 자동 무효화)
[5] E5: 대시보드 화면에 "Last updated / Max date" 반영
```

### 지원 파일 패턴

| 형태 | 예시 | 처리 |
|------|------|------|
| A) 기존 파일 업데이트 버전 | `SSen분석_최종260601.xlsx` | overlap rebuild |
| B) 신규 날짜 파일 | `SSen분석_최종260605.xlsx` | new_only append |

### Idempotency 보장

- 같은 파일/기간을 여러 번 실행해도 중복/오염 없음
- 겹치는 월 파티션은 **재생성(rebuild)** 으로 덮어쓰기 (default)
- `--overlap new_only` 옵션으로 신규 월만 추가 가능

---

## 에픽 E-UPDATE: 원클릭 업데이트 파이프라인

### E-UPDATE 목표

> 신규 Excel 파일 하나를 `data/incoming/`에 넣고 `make update` 한 번으로
> Parquet → DB → 파생테이블 → API → 대시보드 전체가 최신화된다.

### 단계별 상세

#### E-U1: 파이프라인 오케스트레이터
- `src/ssen/pipeline/update_all.py` 구현
- 각 단계 실패 시 롤백 또는 안전한 중단
- `etl_runs` 테이블에 run 기록 (started_at, finished_at, status)

#### E-U2: 증분 감지 로직
- manifest의 `max_date` vs 신규 파일의 `max_date` 비교
- 중복 실행 시 no-op 처리

#### E-U3: 파생테이블 증분 재계산
- lookback_days 파라미터 (기본 30일)로 경계 구간 재계산
- 미래정보 차단: t일 신호 → t+1 종가 진입 기준 엄수

#### E-U4: 캐시 무효화
- dataset_version (= max_date + etl_run_id) 변경 시 캐시 자동 무효화

---

## 데이터 버전 노출

시스템은 항상 아래 경로에서 데이터 최신성을 확인할 수 있어야 한다:

| 위치 | 내용 |
|------|------|
| `data/parquet/_manifest.json` | `last_updated_at`, `last_source_file` |
| DB `etl_runs` 테이블 | `run_id, started_at, finished_at, min_date, max_date, dataset_version, status` |
| API `GET /meta/health` | `last_updated_at`, `max_date`, `dataset_version` |
| 대시보드 UI | "Last updated: … / Max date: …" 상단 표시 |

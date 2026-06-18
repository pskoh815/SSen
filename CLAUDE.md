# SSen 분석 시스템

`SSen분석.xlsx` 기반 서비스 (Parquet / Postgres / 파생테이블 / FastAPI / 대시보드 / 캐시).
신규 데이터가 `data/incoming/`에 들어오면 `.\run.ps1 update` 한 번으로 전 레이어 최신화.
데이터 범위: 2020-01-02 ~ 현재. E1~E7 완료. **현재 에픽 E8: 기간분석 API + 대시보드 탭** (`docs/viz_spec.md`).
**E9 완료: 키움 OpenAPI+ 수집 경로 추가** (`docs/kiwoom_collection_spec.md`) —
data.go.kr 익일 09:00+ 지연을 당일 15:40~16:00로 단축. 거래대금/코스피시세/ADR/OHLCV
4종 모두 전환, daily_update.py 자동화 편입 완료.

## 구현 목표

1. 대용량 최적화 저장/조회 (증분 Parquet + Postgres 파티셔닝/인덱싱)
2. 다수 사용자에게 빠른 API 제공 (FastAPI + derived 테이블 캐시 구조)
3. HTML 대시보드 (기간 입력 → 주도주/매수/매도/갈아타기/근거/수익률 출력)
4. 백테스트 러너: BullBear 매크로 필터 + 깡토 매매시스템 + 눌림목 전략

## 대시보드 실행

`apps/dashboard/index.html` 열기 전 FastAPI 서버 기동 확인:
`.\run.ps1 api_up` (또는 `$env:PYTHONPATH="src"; python -m uvicorn ssen.api.main:app --host 0.0.0.0 --port 8000 --reload`)

## 절대 규칙

- clarifying question 금지. 가정은 `Assumptions` 섹션에 명시 후 진행
- 외부 네트워크/웹 검색 금지 (로컬 파일/레포/터미널만), 단 외부 API(apis.data.go.kr, 키움 OpenAPI+) 호출은 허용
- 파괴적 명령 금지 (`rm -rf`, DB drop 등) → 안전 대안 제시
- 룩어헤드 편향 금지: 신호는 t일 종가 계산 → t+1일 이후 진입. 미래 정보 발견 시 보고 후 수정
- 작은 커밋 단위 (코드 + 테스트 + 문서)
- 에러 시 중단하지 말고 해당 항목 skip + `logs/` 기록
- 반드시 한국어로 답변

## 매 세션 시작 시 자동 점검 (요청 없이 항상 수행)

세션 시작 시, 또는 daily_update.py 관련 작업을 시작하기 전에 다음을 **요청 없이 먼저** 확인하고
이상이 있으면 PLAN에 자동으로 포함시킨다. "전부 정상"이면 한 줄로 보고하고 넘어갈 것
(불필요하게 장문 보고하지 않음).

1. **결측 영업일 자동 백필** (아래 "결측일 자동 복구" 섹션 참조) — fact_daily_stock/
   fact_kospi/fact_adr/market_ohlcv 4종의 max_date가 직전 영업일에 도달했는지 확인하고,
   미달이면 자동으로 백필 절차 실행
2. **이상치 셀프체크** (아래 "셀프체크 루틴" 참조) — 최근 7영업일 데이터에 다음 항목이
   있으면 보고하고 수정 제안:
   - 가격(시가/고가/저가/종가) 컬럼에 음수값
   - 종목명(name)이 NULL/None인 행
   - 거래대금/거래량이 직전 20일 평균의 1000배 이상 또는 1/1000 이하 (단위 변환 누락 의심)
   - 테마/지수 일별 등락률을 복리 누적한 값이 -100% 미만 또는 비현실적 고배수
     (예: 1개월 누적 +1000% 초과) — 이론적으로 불가능한 값
   - dataset_version이 직전 실행보다 과거로 후퇴
3. **신규 데이터 경로 도입 시 체크리스트 준수 확인** (아래 섹션 참조)

## 결측일 자동 복구 (PC 꺼짐 등으로 인한 소급 현행화)

키움 수집은 "오늘"만 호출 가능한 구조(opt10032/opt20006/opt20009/opt10081 전부 당일
스냅샷·당일 차트 기준)이므로, PC가 꺼져 있던 날은 그날의 키움 수집 자체가 원천적으로
불가능하다. 이 경우 **자동으로 data.go.kr 경로로 소급 백필**해야 한다.

- `daily_update.py` 실행 시 매번 먼저 `catalog.json`(또는 manifest)의 max_date와
  직전 영업일을 비교한다. 차이가 1일 이상이면, 그 사이의 모든 영업일(주말/공휴일
  제외)을 누락일로 간주하고 자동으로 다음을 수행한다:
  1. 누락일 목록 산출 (한국 증시 휴장일 캘린더 기준)
  2. 각 누락일에 대해 data.go.kr 경로로 거래대금/코스피시세/ADR/OHLCV 백필
     (data.go.kr 발표 지연으로 아직 공개되지 않은 가장 최근 1~2일은 자동 skip,
     다음 실행 시 재시도)
  3. "오늘" 날짜만 키움 경로로 수집 (정상 경로)
  4. 백필된 날짜는 로그에 `[backfill] {date} via data.go.kr (PC 꺼짐/미실행 추정)`로
     명시 — 사람이 별도로 인지하지 않아도 로그로 추적 가능하게
- 이 로직이 있으면 사람이 "며칠치 밀렸으니 채워줘"라고 매번 요청할 필요가 없다.
  `.\run.ps1 update` 또는 daily_update.py 실행 자체가 항상 "현재까지 빠짐없이"를
  보장해야 한다.
- 단, 백필 시 데이터 성장 규율(증분 처리, catalog.json checksum)을 그대로 따른다 —
  전체 재계산이 아니라 누락 구간만 처리.

## 신규 데이터 경로 도입 시 체크리스트 (회귀 방지)

키움 전환 작업에서 발견된 버그(가격 부호 오염, OHLCV name 필드가 기존 종목명 33.4%
덮어씀, 거래대금/Volume 단위 변환 누락 2건, ETF 41% 혼입, 시장구분 매핑 26% 스킵)는
모두 "새 데이터 경로를 충분한 검증 없이 운영에 편입"한 데서 발생했다. 향후 새로운
데이터 소스/TR/API를 추가하거나 기존 경로를 교체할 때는 다음을 **반드시** VERIFY
단계에 포함한다 (생략 시 DoD 불합격):

1. **단위 검증**: 가격/거래량/거래대금 단위가 기존 컬럼과 일치하는지 외부 소스
   (KRX 공식 통계, investing.com 등 1차 출처)와 최소 1개 날짜 교차대조
2. **부호/이상값 검증**: 가격 음수 0건, 등락률 복리 누적이 -100% 미만이 되는 행 0건
3. **필드 보존 검증**: 기존에 정상적으로 채워져 있던 컬럼(종목명 등)을 신규 경로가
   NULL로 덮어쓰지 않는지 — 신규 경로에 없는 필드는 기존값을 carry-forward하거나
   별도 매핑 테이블에서 채울 것, 무조건 None 대입 금지
4. **혼입 필터링**: 신규 경로가 의도치 않은 항목(ETF/ETN, 관리종목, 타 시장 종목)을
   섞어 줄 가능성을 확인하고 필터링
5. **전체 구간 스캔**: 영향 범위를 추정이 아니라 전체 데이터 스캔(건수 카운트)으로
   확정. "한 종목 사례"가 아니라 "N건/전체M건" 형태로 보고
6. 위 5가지를 모두 통과한 뒤에만 기존 경로를 교체하고, 통과 전에는 신규 경로를
   폴백 없는 단독 경로로 전환하지 말 것

## 환경

| 항목 | 값 |
|---|---|
| 루트 | `C:\MyClaude\ssen-dashboard` |
| 원본 | `data\incoming\SSen분석.xlsx` (157,200행) |
| Parquet | `data/parquet/` (year=YYYY/month=MM 파티션) + `data/catalog.json` |
| DB | `postgresql://ssen:ssen@127.0.0.1:5432/ssen` (서비스: postgresql-x64-17) |
| Redis | `redis://localhost:6379` (캐시, TTL 기본 300s → `SSEN_CACHE_TTL` 환경변수로 조정) |
| API | FastAPI :8000 (`src/ssen/api/main.py`) |
| 대시보드 | `apps/dashboard/index.html` |
| 실행 | `$env:PYTHONPATH="src"` → `python -m ssen.*` 또는 `.\run.ps1 <target>` |
| 키움 수집 환경 | **별도 32bit Python** (`py -3.9-32`) — pandas==2.0.3, numpy<2.0, pykiwoom. 64bit 메인 파이프라인과 분리 실행, AUTO 로그인 등록 완료 |
| 자동화 스케줄 | `scheduler.py`의 `daily_collect` — 매일 16:30 (PC 켜져 있을 때만 동작, 꺼져 있던 날은 위 "결측일 자동 복구"로 보완) |

run.ps1 타겟: `update` / `api_up` / `compact` / `bench_load` / `bench_queries`

Python 3.10+, pandas, polars, duckdb, pyarrow, fastapi, openpyxl, redis. 한국어 주석. 경로는 `pathlib.Path`.

## 코드 구조

```
src/ssen/
  ingest/     excel_to_parquet.py, parquet_to_pg.py
  derived/    calc_derived.py
  collect/
    SSen_Money.py, 지수_상승종목수.py, 코스피지수_시세.py   ← data.go.kr (폴백 + 백필 전용 경로)
    collect_ohlcv.py                                         ← data.go.kr OHLCV (백필/하이브리드 보완)
    kiwoom/
      collect_kiwoom_money.py      ← 거래대금 (당일 전용, parse_price()로 부호 처리)
      collect_kiwoom_kospi.py      ← 코스피시세 (당일+과거 백필 모두 가능)
      collect_kiwoom_adr.py        ← 상승하락비율 (당일 전용)
      collect_kiwoom_ohlcv.py      ← 개별종목 일봉 (당일 전용, universe=거래대금 상위 250일 누적)
  update/
    daily_update.py                ← 결측일 자동 백필 + 키움/data.go.kr 라우팅 통합 진입점
    scheduler.py                   ← daily_collect 매일 16:30 등록
  analysis/   period_analysis.py, perf_timer.py
              supertrend_strategy.py, rs_breakout_strategy.py
  api/        main.py
```

## 성능 규칙 (위반 시 DoD 불합격)

### 데이터 성장 규율
1. xlsx/csv 직접 로딩 금지 — 모든 원천은 ingest → Parquet/DB 경유
2. ingest/derived/backtest는 증분 처리. 전체 재생성은 `--force-rebuild`만
3. catalog.json checksum으로 파티션 변경 감지, 미변경 파티션 skip
4. Pushdown 의무: start/end + 필요 컬럼만 I/O. `scan_parquet` 또는 DuckDB
5. idempotent: 같은 파일 재처리해도 중복/오염 없음 (upsert)

### 성능 예산
- 대표 쿼리 ≤300ms / 1년 백테스트 로딩 ≤2.0s / derived 1개월 증분 ≤30s
- 6년 전체 로딩 금지. 결과는 `docs/perf_report_작업명.md`에 기록
- 전 종목 루프 시: `Series.iloc[i]` 반복 금지 → `.to_numpy()` 후 배열 인덱싱
- 무거운 계산(슈퍼트렌드/볼린저밴드 등) 전, 벡터화 가능한 1차 조건으로 후보 먼저 축소

### API 캐시 규칙 (체감 속도 핵심)
캐시 키: `namespace + dataset_version + {start, end}` 해시 (`_make_key()` 사용).

**병목 엔드포인트** (캐시 MISS 시 10~46초 소요 — 실측):

| 엔드포인트 | MISS | HIT |
|---|---|---|
| supertrend-trades | ~10.5s | 5ms |
| rs-breakout-trades | ~9.7s | 5ms |
| pullback-trades | ~16.5s | 6ms |

이 세 엔드포인트는 **서버 시작 시 자동 워밍업** 필수:
- `api/main.py`의 FastAPI `startup` 이벤트에서 `asyncio.create_task(cache_warmup())` 실행
- `cache_warmup()`: 1M/3M/6M/1Y/YTD 5개 기간 × 3개 엔드포인트 = 15개 조합 사전 계산
- 워밍업은 백그라운드 실행 (서버 응답은 즉시 시작, 약 2~3분 후 전 조합 HIT 상태)
- 사용자가 08:30 이후 서버를 켜므로 새벽 배치 스케줄링 불필요 — startup 이벤트로 대체

**TTL 주의**: 기본 TTL 300s(5분). 종료일을 `today()`로 자동 세팅하는 로직이 있어
날짜가 바뀌면 캐시 키가 달라져 MISS 발생 — 이는 정상 동작이며 워밍업으로 보완.

**성능 계측**: 모든 `/api/period/*` 엔드포인트는 `perf_timer.py`로
`cache/db/calc/serialize/cache_set/total(ms)` 로깅 + `X-Duration-Ms` 응답 헤더 필수.
로그 형식: `[perf] {endpoint} cache={HIT|MISS} db={n}ms calc={n}ms total={n}ms`

## 데이터 수집 경로 (E9: 키움 OpenAPI+)

상세: `docs/kiwoom_collection_spec.md`. 4종 데이터 모두 전환 완료:

| 데이터 | 당일 경로 | 과거 백필 경로 | 단위 변환 |
|---|---|---|---|
| 거래대금 | 키움 opt10032 | data.go.kr | ×1,000,000 (백만원→원) |
| 코스피시세 | 키움 OPT20006 | 키움 OPT20006 (과거도 가능) | 거래량 ×1,000 (천주→주) |
| 상승하락비율(ADR) | 키움 OPT20009/20003 | data.go.kr (키움은 당일만 지원) | 변환 없음 |
| OHLCV(개별종목) | 키움 opt10081 (universe=거래대금 상위 250일 누적) | data.go.kr collect_ohlcv.py | 거래대금 ×1,000,000 |

- **ETF/ETN 반드시 제외**: 거래대금 상위 100종목 중 약 41% 혼입 확인됨. 필터링 로직은
  `kiwoom_collection_spec.md` 참조
- **실행 환경 분리**: 키움 수집 스크립트는 32bit Python(`py -3.9-32`)으로 별도 실행,
  결과 CSV만 64bit 메인 파이프라인(ingest)이 읽음 — 두 환경을 한 프로세스에 섞지 말 것
- **가격 파싱은 반드시 `parse_price()`(절댓값 전용) 사용** — 키움 API가 등락기호(+/-)를
  현재가 필드에 붙여 반환하는데, 이를 가격의 실제 부호로 오인하면 음수 가격이 저장되어
  파생 계산(복리 누적 등)이 -100% 미만 같은 비현실적 값을 만들어낸다 (실제 발생한 회귀)
- **종목명 등 신규 경로에 없는 필드는 무조건 None 금지** — `_load_code_name_map()`
  (fact_daily_stock 기반)으로 항상 채우거나 기존값 carry-forward (실제 318,247행 손상 회귀 발생)

## 데이터셋 키

| 시트(테이블) | key_cols |
|---|---|
| 거래대금 (MostActiveStocks) | 날짜, 종목코드 |
| 테마 (Themes) | 종목코드 |
| 코스피시세 (KospiPrice) | Date |
| 상승하락비율 | 날짜, 지수 |

날짜는 `YYYY-MM-DD` 문자열 통일. 테마 빈셀은 경고만(중단 금지) → `missing_themes.csv` 생성.
**`시각화` 시트는 ingest 제외** — 수식 파생 시트. 해당 로직은 API에서 재구현: `docs/viz_spec.md`.

## 출력 형식

PLAN(≤10불릿) → DO(파일/커맨드) → VERIFY(행수·스키마·성능, 신규 경로 도입 시 위 체크리스트 포함)
→ SUMMARY(변경 파일 + 다음 할 일 ≤5).
샘플 데이터는 상위 20행 + 스키마 + 통계만.

## 상세 스펙 (필요할 때만 읽기)

- `docs/derived_columns.md` — N~AC열 파생 컬럼 계산 로직
- `docs/kkangto_spec.md` — 깡토 매매시스템 v3
- `docs/api_collection.md` — data.go.kr 수집 규약 (폴백 경로)
- `docs/kiwoom_collection_spec.md` — 키움 OpenAPI+ 수집 검증 결과·운영 스펙·발견된 회귀 기록
- `docs/viz_spec.md` — 기간분석 구현 명세
- `docs/dashboard_spec.md` — 대시보드/API 엔드포인트 명세
- `docs/design_system.md` — UI 색상 토큰·차트 규칙 ← **대시보드 UI 수정 시 필독**

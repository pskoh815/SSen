# SSen 분석 시스템

`SSen분석.xlsx` 기반 서비스 (Parquet / Postgres / 파생테이블 / FastAPI / 대시보드 / 캐시).
신규 데이터가 `data/incoming/`에 들어오면 `.\run.ps1 update` 한 번으로 전 레이어 최신화.
데이터 범위: **2015-01-02 ~ 현재** (2026-06-21 KRX OPEN API로 2015~2019 소급 백필 완료,
아래 "KRX OPEN API" 섹션 참조). 단 원본 `SSen분석.xlsx` 적재분(fact_daily_stock)은
여전히 2020-01-02부터이고, 2015~2019는 별도 store(`fact_daily_stock_pre2020`)로
보완하는 이중 구조 — 두 store의 차이를 인지하고 다룰 것. E1~E7 완료.
**현재 에픽 E8: 기간분석 API + 대시보드 탭** (`docs/viz_spec.md`).
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
- 외부 네트워크/웹 검색 금지 (로컬 파일/레포/터미널만)
  - **예외 1**: 외부 API(apis.data.go.kr, 키움 OpenAPI+,OpenDART, OpenKRX API) 호출은 허용함
  - **예외 2**: Anthropic 공식 skills 레포지토리 (`https://github.com/anthropics/skills`)에서 skills를 추가하거나 업데이트할 때는 `git clone` / `git pull`을 허용함.
  - **예외 3**: Claude Code의 `/plugin install` 명령어 사용 시 내부적으로 발생하는 `npm install` / `npx` 네트워크 통신은 허용함.
  - **예외 4**: 제3자(Anthropic 공식이 아닌) GitHub 저장소의 skill/패키지 설치 — 사용자가
    **그 순간 대화에서 정확한 저장소 URL을 직접 제시하며 명시적으로 설치를 요청한 경우에만**
    `git clone` / `npm install` 허용. 추측이나 자동 추천으로 레포를 고르지 말 것 — 항상
    사용자가 명시한 URL만. 사전 포괄 승인이 아니므로, 미리 등록된 레포라도 매번 그 자리의
    명시적 요청이 있어야 함(검증되지 않은 코드 실행 위험이 있어 한 번의 규칙 추가로
    영구 자동승인하지 않기로 함, 2026-06-27 결정).

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
   - 캐시(Redis)에 워밍업 키(1M/3M/6M/1Y/YTD × 병목 엔드포인트)가 현재 dataset_version
     기준으로 존재하는지 — 없으면 워밍업 트리거(startup/daily_collect 체이닝)가
     실패했다는 신호이므로 즉시 수동 트리거하고 원인 보고
   - daily_update.py/daily_collect 로그에 콘솔 출력 관련 에러(UnicodeEncodeError 등)가
     있는지 — 이런 에러 하나가 파이프라인 전체(E1~E3/OHLCV)를 통째로 스킵시킨 사례 있음
3. **신규 데이터 경로 도입 시 체크리스트 준수 확인** (아래 섹션 참조)
4. **기간 포함관계 일관성** — 새로운 집계/선정 로직을 추가할 때, 좁은 기간의 결과가
   넓은 기간(그 좁은 기간을 포함하는) 결과와 모순되지 않는지 확인 (아래 "기간 종속
   계산 금지" 섹션 참조)

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
7. **lookback 의존 지표는 데이터 시작점 부근에서 NaN 구간이 생긴다** — RS 등
   다년 lookback(예: 250거래일)을 쓰는 지표는 데이터 보유 기간 첫 250거래일
   동안 계산 자체가 불가능하다(NaN). 새 데이터 소스로 전환하거나 백테스트
   시작일을 정할 때, "가장 긴 lookback 일수만큼 여유를 둔 과거 데이터"가
   확보되어 있는지 먼저 확인할 것. 실제로 2020년 데이터가 전부 NaN이 되어
   백테스트 결과가 0건으로 나온 사례 있음 — 영업일 기준 250일은 캘린더 기준
   1년이 아니므로(연간 영업일이 248~251일 정도로 해마다 다름), 안전마진을
   두고 최소 3개월 이상 여유를 확보해 백필할 것
8. **다중 패스로 과거 데이터를 백필할 때, 각 패스가 서로 다른 기준 시점의
   수정주가를 줄 수 있다** — 키움 OPT10081처럼 단일 호출이 일정 기간(예: ~600
   거래일)만 반환하는 차트형 TR을 여러 `기준일자`로 나눠 호출해 긴 구간을
   이어붙일 경우, 수정주가가 "호출 시 넘긴 기준일자까지 알려진 이벤트만"
   반영되는 경우가 있다. 두 패스의 기준일자 사이에 실제 분할/병합이 있었던
   종목은 패스 경계에서 인위적인 가격단절이 생긴다(2026-06-20 발견: 1247종목
   3패스 백필 중 43건, 삼성전자 50:1 분할 등). **패스 경계마다 가격단절
   재스캔(인접 등장일 비율 4배 이상/0.25배 이하) 필수** — 발견 시 키움 HTS
   등 공식 화면과 직접 대조해서 진짜 분할/병합인지 검증한 후, 경계 비율을
   그 종목의 경계 이전 구간 전체(가격 컬럼만)에 곱해 보정할 것. 거래정지로
   인접일에 거래량=0인 경우 비율 계산에 그 날짜를 쓰지 말고 거래량>0인
   가까운 날 기준으로 다시 계산(단, 거래정지가 1년 이상 길면 인접일 대체
   시 실제 시장 재평가가 섞여 들어가므로 경계일 원값을 그대로 쓰는 게 더 안전).

## 기간 종속 계산 금지 (모멘텀/순위/추세 지표 설계 시)

종목·테마의 "변화"나 "모멘텀"을 계산할 때, **조회 구간의 절대적 시작/끝/중간
날짜를 외부 기준점으로 쓰지 말 것**. 실제 발생한 회귀: 강세 주도종목의
순위모멘텀이 "조회 기간의 중간 날짜"를 기준으로 전반/후반을 나눠 비교했는데,
이 mid 값이 종목 자신의 등장 이력과 무관하게 사용자가 고른 start/end에만
의존했다. 그 결과 같은 종목·같은 원자료인데도 조회 기간을 좁히면 mid가
달라져 전혀 다른(심지어 반대) 결과가 나왔다 — 좁은 기간(부분집합)에서는
탈락하는데 넓은 기간(상위집합)에서는 선정되는 모순이 발생.

**원칙**: 추세/모멘텀은 항상 **그 종목·테마 자신의 연속 등장 시퀀스 내부에서만**
비교한다 (예: 연속 등장일 사이의 순위 상승/하락 전환 횟수 비율). 조회 구간을
어디서 자르든, 그 구간 안에 포함된 동일 데이터에 대한 결과는 일관되어야 한다.
새로운 집계 지표를 설계할 때 "이 계산이 조회 시작일/종료일의 정확한 위치에
따라 결과가 바뀌는가?"를 자문하고, 그렇다면 외부 기준점 의존을 제거할 것.

| 항목 | 값 |
|---|---|
| 루트 | `C:\MyClaude\ssen-dashboard` |
| 원본 | `data\incoming\SSen분석.xlsx` (157,200행) |
| Parquet | `data/parquet/` (year=YYYY/month=MM 파티션) + `data/catalog.json` |
| DB | `postgresql://ssen:ssen@127.0.0.1:5432/ssen` — **Docker 컨테이너** `ssen_postgres`(이미지: postgres:17-alpine)로 운영. 서비스로 등록되어 있지 않으므로 `Get-Service`로 찾으면 안 나옴. pg_dump 등 클라이언트 도구도 Windows에 설치되어 있지 않으면 `docker exec ssen_postgres pg_dump -U ssen -d ssen > backup.sql` 형태로 컨테이너 내부 실행 |
| Redis | `redis://localhost:6379` — Docker 컨테이너 `ssen_redis`(이미지: redis:7-alpine). 캐시, TTL 기본 300s → `SSEN_CACHE_TTL` 환경변수로 조정 |
| API | FastAPI :8000 (`src/ssen/api/main.py`) |
| 대시보드 | `apps/dashboard/index.html` |
| 실행 | `$env:PYTHONPATH="src"` → `python -m ssen.*` 또는 `.\run.ps1 <target>` |
| 키움 수집 환경 | **별도 32bit Python** (`py -3.9-32`) — pandas==2.0.3, numpy<2.0, pykiwoom. 64bit 메인 파이프라인과 분리 실행, AUTO 로그인 등록 완료 |
| 자동화 스케줄 | `scheduler.py`의 `daily_collect` — 매일 16:30 (PC 켜져 있을 때만 동작, 꺼져 있던 날은 위 "결측일 자동 복구"로 보완). 완료 직후 `cache_warmup()` 체이닝 필수(아래 API 캐시 규칙 참조). 콘솔 stdout/stderr는 UTF-8 강제 설정(`main.py` startup) — 인코딩 에러로 배치 전체가 스킵된 사례 있음 |
| 워치독 | `collect_kiwoom_ohlcv.py`에 적용됨(30초 무진행 시 강제종료, 키움 중복 로그인으로 인한 54분 행 사고 후 추가). money/kospi/adr 3개 수집기는 `subprocess.run(timeout=180s)` 상위 안전장치만 있음 — 동일 워치독 적용 권장 |

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

이 세 엔드포인트는 **dataset_version이 바뀌는 모든 시점에 자동 워밍업** 필수.
시간(cron) 기반 트리거는 금지 — PC가 그 시각에 꺼져 있으면 영구 누락되고, 서버를
며칠씩 띄워둬도 다음 cron까지 갱신이 안 되는 두 가지 실패 모드를 모두 겪었음.
트리거는 정확히 2곳으로 고정:
1. `api/main.py`의 FastAPI `startup` 이벤트 — 서버가 언제 켜지든 1회 실행
2. `daily_collect`(scheduler.py) 완료 직후 체이닝 — daily_update.run()이
   dataset_version을 갱신하므로, 그 직후 곧바로 워밍업이 따라가야 함
   (이게 빠지면 서버를 안 내려도 매일 16:30마다 다시 MISS 발생)
- `cache_warmup()`: 1M/3M/6M/1Y/YTD 5개 기간 × 3개 엔드포인트 = 15개 조합 사전 계산
- 워밍업은 백그라운드 실행 (서버 응답은 즉시 시작, 약 3분 후 전 조합 HIT 상태)
- 새로운 트리거 지점(예: 다른 배치 작업)을 추가할 때도 "dataset_version을 바꾸는
  모든 지점은 워밍업을 체이닝해야 한다"는 원칙을 적용할 것

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

## KRX OPEN API (E10 완료: 2015~2019 소급 백필)

`https://data-dbg.krx.co.kr/svc/apis/sto/{stk,ksq}_bydd_trd` (유가증권/코스닥 일별매매정보,
헤더 `AUTH_KEY`=`.env`의 `OPENKRX_KEY`)로 2026-06-21 **2015-01-01~2019-12-31 전 거래일
(1,227일) × KOSPI/KOSDAQ 전종목**을 백필 완료. 가격은 원시가(수정주가 미반영, 기존
`fact_daily_stock`/`market_ohlcv`와 동일 컨벤션). 수집기: `src/ssen/collect/krx_open_api.py`
(일자별 JSON 캐시, 재실행 시 캐시 히트로 API 재호출 없음).

**구현 내역**:
1. `data/market/ohlcv`에 그 기간 거래대금 상위 50(코스피+코스닥) 중 기존 키움 백필
   universe(866종목, "현재 거래중" 한정)에 없던 **1,270개 종목**(주로 상장폐지/합병)을
   추가 — 서바이버십 편향 대폭 완화. 거래정지/단일가매매(OHL=0, close>0)는
   open=high=low=close로 보정
2. `dim_theme`에 신규종목 테마(1차) 결측 145건을 사용자 수동 입력으로 채움(CSV
   왕복 시 Excel이 종목코드 앞자리 0을 날리는 함정 발견 — `code.zfill(6)`으로 복구,
   종목명 대조로 무손실 확인됨)
3. KRX 캐시(전종목 raw) + `calc_derived.add_derived_columns()`(기존 함수 **그대로 재사용**)
   + `dim_theme` + `fact_kospi`(2014-07-25~ 백필분)로 `fact_daily_stock`과 **완전히
   동일한 스키마/dtype**의 2015~2019 파생 데이터를 산출해 `data/parquet/fact_daily_stock_pre2020/`
   저장 (60개 월 파티션, 259만행)
4. `period_analysis.py`의 `_FDS`를 `fact_daily_stock_pre2020`+`fact_daily_stock`
   멀티글롭(`read_parquet([...])`)으로 확장 — 압도적 테마종목/강세주도종목/이벤트/
   theme-rank-days 등 fact_daily_stock 의존 위젯 전부가 코드 변경 없이 2015년부터 지원됨
5. `backtest.py`의 `_load_parquet()`도 `fact_daily_stock_pre2020`을 같이 스캔하도록
   확장 — "트레이드 로그"/"주도 테마 레짐 타임라인"(Postgres `derived_*` 테이블, E3
   백테스트 결과)도 2015~2019 재계산해 지원

**주의**: 새 fact_daily_stock류 데이터 디렉터리를 추가할 때 라우팅 지점이
**두 곳**(`period_analysis.py`의 `_FDS`, `backtest.py`의 `_load_parquet()`)으로
나뉘어 있다 — 한쪽만 고치고 잊으면 그 경로를 쓰는 위젯만 조용히 옛 범위로 남는다.

**남은 한계**: `fact_kospi`(코스피지수, 시장조정 Alpha용)는 2014-07-25부터 백필됐고,
`fact_adr`(ADR)도 2026-06-24 같은 raw_cache(FLUC_RT 필드)에서 추가 API 호출 없이
계산해 2015-01-02~2019-12-30(1,227거래일×2시장=2,454행)까지 백필 완료 — `data/parquet/
fact_adr/`는 fact_daily_stock과 달리 사전 분리된 store가 필요 없어(소비처가 항상
`yearmonth=*/data.parquet` 전체 글롭) 기존 디렉터리에 그대로 추가, `is_verified`
컬럼은 2020-01 등 과거 파티션과 동일하게 생략(확정치라 검증 대기 개념 자체가 없음).
Postgres에는 동기화하지 않음(2015~2019 fact_daily_stock도 Postgres에 안 올리는 기존
결정과 동일 — 운영 백테스트(`backtest.py`/E3)는 parquet 직접 읽기라 영향 없고, ADR을
쓰는 코드(`backtest_pullback.py`)도 parquet 직접 글롭이라 자동으로 인식됨). `PRE2020_NOTICE`(테마 소급
적용/서바이버십 편향 안내)는 `supertrend/rs-breakout/pullback`(market_ohlcv 기반,
universe="현재 거래중" 한정이라 서바이버십 편향 여전)에는 그대로 유효 — 1,270개 종목
추가는 `fact_daily_stock_pre2020` 경로(테마/리더/레짐/깡토트레이드)에만 해당.

추가 미연동 엔드포인트(참고용, 실제 도입 시 위 "신규 데이터 경로 체크리스트" 적용):
ETF 일별매매정보, 유가증권/코스닥 종목기본정보(상장폐지·종목코드 변경 등 corp
메타데이터 보강에 활용 가능).

## market_ohlcv 시작일 + fact_ma_breadth (2026-06-25)

`market_ohlcv`를 KRX OPEN API로 **2014-01-02까지 소급 백필**(기존 시작일 2014-07-25에서
확장, 266,913행) — 200일 이동평균 워밍업 버퍼를 2015-01-02부터 즉시 확보하기 위함.
전체 범위: **2014-01-02 ~ 현재**, 끊김 없음.

신규 store `data/parquet/fact_ma_breadth/`(date, total_count, below20_count,
below200_count, ratio20, ratio200) — "20일선/200일선 이탈비율"(전체 상장종목 중 자기
이동평균 아래인 종목 비율, 스톡이지 "시장지표" 위젯과 동일 개념) 2015-01-02~현재 백필
완료. `market_ohlcv` 확장 덕분에 ratio200도 2015-01-02부터 NaN 없이 유효. 매크로
필터로 A/B 테스트해본 결과: 코스피EMA20/60·BR5(BullBear)·20일선이탈비율 전부 종목
단위 추세전략(EMA60>120>240+9/26/52일 중간값 정배열+지지선)에는 수익을 깎았음(필터
없는 원본이 10년 복리 기준 항상 최고) — 시장 전체 필터와 종목 자체의 추세 확인 조건이
중복/상충되는 경향, 다른 전략에 적용하기 전 반드시 개별 A/B 테스트할 것.

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

## 알려진 과거 회귀 (재발 방지용 기록)

아래는 실제로 발생했던 회귀와 그 근본 원인. 비슷한 구조의 코드를 작성하기 전에
한 번씩 떠올릴 것:

- **`_delete_derived()`류 "재계산 시 기존 행 삭제 후 재삽입" 로직의 함정**: 삭제
  조건에 그 run이 만들어낼 새 dataset_version(아직 DB에 존재하지 않는 값)을
  쓰면 삭제가 영원히 0건 매칭되어 매번 중복이 쌓인다. 삭제 조건은 날짜·자연키
  등 불변 식별자만 사용할 것, 이번 run에서 생성될 버전 값을 조건에 넣지 말 것
- **조회 함수의 `dataset_version = %s` exact-match**: 좁은 lookback으로 증분
  재계산할 때마다 새 버전이 찍히면, 과거 풍부한 데이터가 DB에 살아있어도 최신
  버전과 일치하지 않아 영영 조회 불가능해진다. 조회는 항상 "자연키 기준 최신
  버전 행만 남기는 dedup" 방식을 쓸 것, exact-match 필터 금지
- **모멘텀/추세 계산의 외부 기준점 의존** — 위 "기간 종속 계산 금지" 섹션 참조
- **dataset_version 갱신 시점이 실제 데이터 준비 완료보다 빠른 레이스 컨디션**:
  daily_update.py의 여러 단계(거래대금/코스피/ADR ingest, OHLCV 수집, dataset_version
  갱신) 순서가 잘못되면, dataset_version이 바뀐 직후 ~ 모든 데이터가 실제로 채워지기
  전 사이의 짧은 틈에 들어온 조회가 "불완전한 상태"를 그 dataset_version 키로
  영구 캐싱해버린다. 캐시는 dataset_version만 보고 "최신"이라 믿으므로, 다음 날
  dataset_version이 또 바뀌기 전까지 이 불완전한 캐시가 절대 갱신되지 않는다.
  원칙: dataset_version 갱신은 **그 버전에 의존하는 모든 데이터 소스(OHLCV 포함)가
  실제로 다 준비된 후 맨 마지막에** 수행할 것 — 단계를 추가할 때마다 이 순서를
  재확인할 것
- **액면병합/분할로 인한 가격 단절이 수익률 계산을 왜곡**: `fact_daily_stock`(거래대금
  TR 기반, OPT10032/data.go.kr 거래대금 경로)은 "당일 시세 스냅샷"이라 구조적으로
  수정주가 개념이 없어 원시 가격이 그대로 들어간다. 반면 `data/market/ohlcv`(OPT10081,
  수정주가구분="1")는 이미 정상적으로 수정주가가 반영되어 있다 — **같은 시스템 안에서
  두 store가 가격 처리 방식이 다르다**는 점을 항상 기억할 것. 테마/종목 기간수익률처럼
  fact_daily_stock 기반 가격 비교를 새로 만들 때는, 액면병합·분할로 인한 비현실적
  단일종목 수익률(예: 10:1 병합 시 +900%대)이 섞이지 않도록 가드가 필요하다.
  실제 적용된 가드: 인접 등장일 가격 비율이 4배 이상(±300% 상당) 벌어지면 그 종목을
  해당 집계에서 제외. 전체 소급 수정주가 적용은 비용 대비 효과가 낮음(희소 사례,
  병합비율 공시 대조 필요) — 가드 + 발견 시 건별 보정의 하이브리드가 더 실용적
- **인적분할/상호변경(종목명 변경)으로 보존된 가치가 백테스트에서 "손실"로 잘못
  계산됨**: 2017-12-08 BGF리테일 인적분할(지주사 BGF 분리) 시 같은 종목코드(027410)의
  가격이 79,100원→15,350원으로 4일에 걸쳐 단계적으로 재평가됐는데(일별 등락률
  -29.94%/-29.95%/-18.0%/-6.4%, 가격제한폭 안쪽이라 위 "가격단절(4배+)" 가드에도
  안 걸림), `backtest.py`의 `compute_trades()`가 이를 -80.59%의 실손실로 계산해
  242건 중 1건이 전체 백테스트 누적수익률을 -85%→실제로는 -23%로 왜곡시켰다(평균
  수익률 부호까지 반전). 실제로는 분할로 받은 신주(별도 종목코드)를 함께 보유하므로
  자산가치는 거의 보존됨 — 분할비율을 모르는 한 정확한 보정이 불가능하므로, 보유기간
  중 그 종목의 **종목명(name)이 바뀌면 해당 트레이드를 결과에서 제외**하는 가드를
  추가(정상적인 급등주는 종목명이 안 바뀌므로 오탐 없음 — 같은 날 +29.71%인 정상
  급등 사례로 교차검증됨). 새로운 백테스트/지표를 종목코드 단위로 추적할 때마다
  "그 코드가 보유기간 중 다른 회사로 바뀔 수 있는가"를 자문할 것
- **월 단위 파티션에 부분 기간만 수집해도 기존 파일을 통째로 덮어써 나머지 날짜가
  삭제됨**: `collect_ohlcv.py`(data.go.kr OHLCV)의 `to_parquet()`가 `df.to_parquet()`로
  무조건 덮어쓰는 구조였는데, `yearmonth=YYYYMM/{market}/data.parquet` 파일 하나에
  그 달 전체가 들어있어서 narrow한 날짜 범위(예: 최근 1~9일)만 새로 수집해도 같은
  달의 나머지 날짜가 통째로 사라진다. 2026-06-17 스케줄러에 OHLCV 수집이 처음
  정식 연결되면서(그 전엔 별도 수동 스크립트가 항상 전체 월 단위로 돌려서 드러나지
  않았음) `daily_update.py`가 매일 narrow한 `target_dates`로 `run_ohlcv_datagokr_backstop()`을
  호출 → 매일 밤 그 달의 다른 날짜가 조금씩 깎여나가는 회귀가 9일간(06-17~06-25)
  발생, `market_ohlcv` 종목수가 2,771→1,241로 절반 이하로 줄었음(2026-06-25 발견,
  KRX OPEN API로 전체 재수집해 복구). **수정**: `to_parquet()`이 기존 파일이 있으면
  `(date, code)` 기준 upsert 병합(신규가 기존을 덮어씀) 후 저장하도록 변경 —
  narrow한 날짜 범위로 호출해도 같은 달의 다른 날짜를 더 이상 지우지 않음. **월
  파티션(파일 하나에 여러 날짜)에 쓰는 모든 신규 수집기는 "부분 기간만 다시 써도
  안전한가"를 반드시 확인할 것** — 매일 호출되는 배치일수록 위험이 큼(매번 narrow한
  범위로 호출되기 때문)
# API 문서 (E4)

## 기본 정보

| 항목 | 값 |
|------|-----|
| Base URL | `http://localhost:8000` |
| Swagger UI | `http://localhost:8000/docs` |
| ReDoc | `http://localhost:8000/redoc` |
| 기동 명령 | `.\run.ps1 api_up` 또는 `make api_up` |

## 공통 응답 구조

모든 응답에 `meta` 필드 포함:
```json
{
  "meta": {
    "api_version": "1.0",
    "dataset_version": "2026-05-29",
    "generated_at": "2026-06-04T06:00:00Z"
  },
  "data": { ... }
}
```

## 엔드포인트

### GET /health
서비스 상태 확인.

### GET /meta/dataset
데이터셋 최신성 정보 (대시보드 상단 표시용).
```json
{
  "dataset_version": "2026-05-29",
  "last_updated_at": "2026-06-04T14:51:47+09:00",
  "min_date": "2020-01-02",
  "max_date": "2026-05-29"
}
```

### GET /leaders/daily?date=YYYY-MM-DD&rule_version=v1.0
특정 날짜의 주도 테마 + 주도주.
- `date`: 조회 날짜 (신호일 t)
- **주의**: 체결은 t+1 거래일 close_price 기준

### GET /leaders/regimes?start=&end=&rule_version=&limit=
기간 내 주도 테마 레짐 목록.
- `start`, `end`: 날짜 범위
- `limit`: 최대 200건

### GET /trades?start=&end=&code=&theme=&rule_version=&limit=
트레이드 로그 + 성과 요약.
- `code`: 종목코드 필터 (선택)
- `theme`: 테마명 필터 (선택)
- 응답에 `summary` 포함 (승률, MDD, 평균 수익률 등)

### GET /stocks/{code}/summary?start=&end=
종목 기간 요약 (출현일수, 평균순위, 트레이드 결과).

### GET /themes/{theme}/summary?start=&end=
테마 기간 요약 (레짐 횟수, 평균 지속일, 승률).

### GET /perf?n=30
대표 쿼리 p50/p95/p99 응답시간 측정.

## 캐시 전략

- **현재**: `cachetools.TTLCache` (in-memory, TTL=300초, maxsize=512)
- **무효화**: `dataset_version`이 변경되면 캐시 키 미스 → 자동 갱신
- **Redis 전환**: `src/ssen/api/cache.py`의 `cache_get`/`cache_set` 함수만 교체

## SQL 인젝션 방지

- 모든 쿼리는 psycopg2 파라미터 바인딩 (`%s`) 사용
- 동적 테이블명/컬럼명 없음
- `rule_version`, `dataset_version` 등 문자열도 전부 바인딩

## 샘플 curl

```bash
# 헬스체크
curl http://localhost:8000/health

# 데이터셋 정보
curl http://localhost:8000/meta/dataset

# 2025년 1분기 레짐
curl "http://localhost:8000/leaders/regimes?start=2025-01-01&end=2025-03-31"

# 2025년 트레이드 로그
curl "http://localhost:8000/trades?start=2025-01-01&end=2025-12-31"

# 특정 날짜 주도주
curl "http://localhost:8000/leaders/daily?date=2025-01-02"

# 종목 요약 (클로봇)
curl "http://localhost:8000/stocks/466100/summary?start=2025-01-01&end=2025-12-31"

# 테마 요약 (반도체)
curl "http://localhost:8000/themes/%EB%B0%98%EB%8F%84%EC%B2%B4/summary?start=2020-01-01&end=2026-05-29"
```

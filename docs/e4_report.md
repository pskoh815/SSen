# E4 Report - FastAPI 서버

Generated: 2026-06-04

## 엔드포인트 구현 현황

| 엔드포인트 | 상태 | 설명 |
|-----------|------|------|
| GET /health | ✅ | 서비스 헬스체크 |
| GET /meta/dataset | ✅ | 데이터셋 버전/최신성 정보 |
| GET /leaders/daily | ✅ | 특정 날짜 주도 테마+주도주 |
| GET /leaders/regimes | ✅ | 기간 레짐 목록 |
| GET /trades | ✅ | 트레이드 로그 + 성과 요약 |
| GET /stocks/{code}/summary | ✅ | 종목 기간 요약 |
| GET /themes/{theme}/summary | ✅ | 테마 기간 요약 |
| GET /perf | ✅ | 응답시간 측정 |

## p95 응답시간 (n=30, 캐시 미적용)

| 엔드포인트 | p50 | p95 | p99 | max |
|-----------|-----|-----|-----|-----|
| GET /leaders/regimes (2025년) | 0.9ms | **2.4ms** | 2.63ms | 2.64ms |
| GET /trades (2025년) | 0.62ms | **0.9ms** | 1.16ms | 1.25ms |
| GET /meta/dataset | 0.14ms | **0.16ms** | 0.25ms | 0.28ms |

캐시 적중 시 0.05ms 이하 예상.

## 샘플 응답

### GET /meta/dataset
```json
{
  "meta": {"api_version": "1.0", "dataset_version": "2026-05-29"},
  "data": {
    "dataset_version": "2026-05-29",
    "min_date": "2020-01-02",
    "max_date": "2026-05-29"
  }
}
```

### GET /leaders/daily?date=2025-01-02
```json
{
  "data": [{
    "date": "2025-01-02",
    "theme1": "로봇",
    "leader_code": "466100",
    "leader_name": "클로봇",
    "leader_rank": 1
  }]
}
```

### GET /trades?start=2025-01-01&end=2025-06-30 (summary)
```json
{
  "total": 27,
  "summary": {
    "win_rate_pct": 25.9,
    "avg_net_pnl_pct": 0.46,
    "max_drawdown_pct": -11.68
  }
}
```

### GET /themes/반도체/summary
```json
{
  "data": {
    "regime_count": 156,
    "avg_duration_days": 5.8,
    "win_rate_pct": 35.3
  }
}
```

## 캐시 & SQL 보안

- TTLCache(TTL=300s), dataset_version 변경 시 자동 무효화
- 모든 SQL 파라미터 바인딩 (인젝션 방지)
- Redis 전환: `cache.py`의 `cache_get`/`cache_set`만 교체

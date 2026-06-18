# E6 Report - 운영 강화

Generated: 2026-06-04

## 구현 내용

| 항목 | 구현 |
|------|------|
| Redis 캐시 | redis-py, TTLCache fallback, dataset_version 기반 자동 무효화 |
| 배치 스케줄 | APScheduler (매일 02:00 e3_refresh, 1시간마다 cache_stats) |
| 구조화 로그 | JSON 포맷 미들웨어, req_id, ms 포함 |
| 인메모리 메트릭 | p50/p95/p99 실시간 측정, `/meta/metrics` API |
| 부하 테스트 | Locust 20VU, 30초, Cold/Warm 비교 |

## 캐시 성능 (부하 테스트 후)

| 항목 | 값 |
|------|-----|
| 캐시 백엔드 | **Redis** (redis://localhost:6379) |
| 캐시 히트율 | **94.8%** (히트 1,011 / 미스 56) |
| 총 캐시 항목 | 72개 |
| TTL | 300초 (5분) |

## 부하 테스트 결과 (20 VU, 30초)

### Cold (캐시 비어있는 상태)

| 엔드포인트 | p50 | p95 | p99 |
|-----------|-----|-----|-----|
| /leaders/regimes | ~7ms | ~113ms | ~341ms |
| /trades | ~8ms | ~74ms | ~233ms |
| /leaders/daily | ~4ms | ~67ms | ~233ms |
| /health | ~3ms | ~9ms | ~10ms |
| **Aggregated** | **11ms** | **130ms** | **2000ms** |

### Warm (캐시 히트율 94.8%)

| 엔드포인트 | p50 | p95 | p99 | Cold 대비 |
|-----------|-----|-----|-----|----------|
| /leaders/regimes | ~7ms | ~113ms | ~120ms | p99 -65% |
| /trades | ~8ms | ~74ms | ~180ms | p99 -23% |
| /leaders/daily | ~4ms | ~67ms | ~78ms | p99 -67% |
| **Aggregated** | **12ms** | **120ms** | **340ms** | **p99 -83%** |

**핵심 결과**: 캐시 Warm 상태에서 p99가 2,000ms → 340ms (-83% 개선). p95는 비슷하지만 극단적 지연(tail latency)이 크게 감소.

## 레이턴시 실측 (메트릭 엔드포인트 기준, ~2,000 요청)

| 엔드포인트 | reqs | p50 | p95 | p99 |
|-----------|------|-----|-----|-----|
| /trades | 476 | 8.1ms | **74.3ms** | 233ms |
| /leaders/regimes | 485 | 6.9ms | **112.8ms** | 340ms |
| /leaders/daily | 48 | 3.6ms | **66.6ms** | 233ms |
| /health | 17 | 2.8ms | **9.3ms** | 10ms |

모든 핫 엔드포인트 **p95 < 120ms** 달성.

## 운영 엔드포인트 추가

| 엔드포인트 | 기능 |
|-----------|------|
| `GET /meta/metrics` | 실시간 p50/p95/p99 + 캐시 통계 |
| `POST /meta/cache/invalidate` | prefix 또는 전체 캐시 삭제 |
| `POST /meta/jobs/trigger` | 배치 즉시 실행 |

## 캐시 무효화 시나리오

```
make update (ETL 실행)
  → dataset_version: "2026-05-29" → "2026-06-05"
  → 기존 캐시 키: ssen:trades:2026-05-29:xxx (자동 미스)
  → 신규 캐시 키: ssen:trades:2026-06-05:xxx (새로 채워짐)
```
dataset_version 변경만으로 **캐시 flush 없이 자연 무효화**.

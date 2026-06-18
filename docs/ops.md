# 운영 가이드 (E6)

## 서비스 구성

| 컴포넌트 | 포트 | 시작 |
|---------|------|------|
| FastAPI API 서버 | 8000 | `.\run.ps1 api_up` |
| PostgreSQL 17 | 5432 | Windows 서비스 자동 시작 |
| Redis | 6379 | choco install redis-64 (자동 서비스) |

## 캐시 구조

### 캐시 키 형식
```
ssen:{namespace}:{dataset_version}:{params_hash}
```
예: `ssen:leaders_regimes:2026-05-29:a3f2b91c`

### dataset_version 기반 자동 무효화
ETL(`make update`) 실행 → `dataset_version` 갱신 → 기존 캐시 키 자동 미스

### 수동 무효화
```bash
# 특정 prefix 무효화
curl -X POST "http://localhost:8000/meta/cache/invalidate?prefix=leaders"
# 전체 캐시 삭제
curl -X POST "http://localhost:8000/meta/cache/invalidate"
```

### Redis 환경변수
```
SSEN_REDIS_URL=redis://localhost:6379   # 기본값
SSEN_CACHE_TTL=300                      # TTL 초 (기본 5분)
```
Redis 미연결 시 TTLCache(in-memory)로 자동 fallback.

## 배치 스케줄

| 작업 | 주기 | 내용 |
|------|------|------|
| e3_refresh | 매일 02:00 (KST) | 파생 테이블 재계산 + 캐시 무효화 |
| cache_stats | 1시간마다 | 캐시 통계 로그 |

### 수동 트리거
```bash
curl -X POST "http://localhost:8000/meta/jobs/trigger?job_id=e3_refresh"
```

## 모니터링

### 메트릭 조회
```bash
curl http://localhost:8000/meta/metrics
```
응답: 엔드포인트별 p50/p95/p99, 캐시 히트율, 요청 수

### 로그 형식 (JSON 구조화)
```
2026-06-04 15:49:30 ssen.api INFO {"req_id":"5d9844e6","method":"GET","path":"/health","qs":"","status":200,"ms":8.1}
```

### 응답 헤더
- `X-Request-Id`: 요청 추적 ID
- `X-Response-Time-Ms`: 응답 시간(ms)

## 부하 테스트

```powershell
cd ssen-dashboard

# Cold 테스트 (캐시 비어있는 상태)
locust -f tests/load/locustfile.py --host http://localhost:8000 `
    --users 20 --spawn-rate 5 --run-time 60s --headless `
    --html tests/load/report.html

# 결과 확인
Start-Process tests/load/report.html
```

## 원클릭 업데이트 파이프라인

```powershell
# 1. 새 Excel 파일 넣기
Copy-Item "C:\MyPy\SSen분析.xlsx" ".\data\incoming\"

# 2. 전체 업데이트
.\run.ps1 update
# → E1(변환) → E2(적재) → E3(파생테이블) → 캐시 자동 무효화

# 3. 완료 후 캐시 즉시 무효화 (선택)
curl -X POST "http://localhost:8000/meta/cache/invalidate"
```

## Docker Compose (docker 환경)

```yaml
# docker-compose.yml 참조
# make db_up → docker compose up -d
# Redis 서비스 포함
```

## 장애 대응

| 증상 | 원인 | 조치 |
|------|------|------|
| API 응답 느림 | Redis 연결 끊김 | `GET /health`로 cache_backend 확인, Redis 재시작 |
| 캐시 히트율 낮음 | TTL 만료, 다양한 쿼리 | SSEN_CACHE_TTL 증가 |
| 데이터 오래됨 | ETL 미실행 | `.\run.ps1 update` |

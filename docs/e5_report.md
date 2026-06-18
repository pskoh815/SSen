# E5 Report - HTML 대시보드

Generated: 2026-06-04

## 구현 방식

| 항목 | 선택 | 이유 |
|------|------|------|
| 프레임워크 | 단일 HTML (Vanilla JS) | 빌드 불필요, FastAPI 직접 서빙 |
| 차트 | Chart.js CDN | 설치 없이 타임라인 렌더링 |
| 스타일 | 인라인 CSS (다크 테마) | 외부 의존성 최소화 |
| 서빙 | FastAPI `/dashboard` 라우트 | 별도 서버 불필요 |

## 구성 파일

| 파일 | 크기 | 내용 |
|------|------|------|
| `apps/dashboard/index.html` | 25.8KB | 전체 대시보드 (HTML+CSS+JS) |

## 화면 구성 요소

| 컴포넌트 | 기능 |
|---------|------|
| 헤더 메타 배지 | dataset_version, last_updated_at, max_date 표시 |
| 기간 선택기 | 날짜 입력 + 3개월/6개월/1년/전체 단축 버튼 |
| 성과 카드 5개 | 총 거래, 승률, 평균수익, 누적수익, MDD |
| 레짐 타임라인 | CSS 기반 가로 막대, 테마별 색상, 클릭 필터링 |
| 트레이드 테이블 | 정렬/검색/필터, 컬럼 클릭 정렬 |
| 근거 패널 | 5단계 자동 근거 생성 (DB 추가 쿼리 없음) |

## Hard Requirements 충족

| 요구사항 | 구현 |
|---------|------|
| 첫 로드 시 기본 기간 (6개월) | ✅ `init()` 함수에서 자동 설정 + 조회 |
| 네트워크 실패 처리 | ✅ try/catch → 상단 오류 메시지 표시 |
| 빈 결과 처리 | ✅ empty/placeholder 상태 표시 |
| 동일 기간 재조회 캐싱 | ✅ `Map<"start|end", response>` 클라이언트 캐시 |
| /meta/dataset 정보 표시 | ✅ 헤더에 max_date, last_updated_at, version |
| start/end 변경 시 즉시 갱신 | ✅ 조회 버튼 또는 Enter 키 |

## 접속 방법

```powershell
.\run.ps1 ui_up
# → http://localhost:8000/dashboard
```

## API 호출 패턴

대시보드 1회 조회 시:
1. `GET /meta/dataset` — 헤더 정보 (페이지 로드 1회)
2. `GET /leaders/regimes?start=&end=` — 타임라인
3. `GET /trades?start=&end=` — 테이블 + 성과 카드

총 3개 API 호출, 동일 기간 재조회 시 0개 (클라이언트 캐시).

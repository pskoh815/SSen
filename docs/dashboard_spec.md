# 대시보드 / API 명세

## FastAPI 엔드포인트

```
GET  /meta/health        last_updated_at, max_date, dataset_version
GET  /api/themes         테마별 누적 등락률+거래대금 시계열 (?start&end&min_volume)
GET  /api/stocks         종목별 기여점수·등락률·거래대금, 버블차트용 (?start&end&theme)
GET  /api/market         상승하락비율·코스피시세·ADR (?start&end)
GET  /api/themes/list    전체 테마 목록
POST /api/themes/update  테마 빈셀 수동 보완 (JSON body)

# 기간분석 (E8, 상세: viz_spec.md)
GET  /api/period/theme-trend    테마 누적복리 시계열 (?start&end&top)
GET  /api/period/leaders        강세 주도종목 TOP30 (?start&end)
GET  /api/period/themes         상승/하락 테마 TOP10 (?start&end&n)
GET  /api/period/breadth        규모별·시장별 강세 분포 (?start&end)
GET  /api/period/leader-events  기여점수≥7 이벤트 (?start&end)
```

DB `etl_runs` 테이블 필수: run_id, started_at, finished_at, input_files,
min_date, max_date, dataset_version, status. 캐시는 dataset_version 연동 무효화.

## 대시보드 (dashboard/index.html)

- 디자인: `docs/design_system.md` 준수 (딥 네이비 #04111f 계열, Pretendard,
  상승=주황 #e05e30 / 하락=청 #3a9fd8 한국 관례, 펄스 닷·글로우 칩 시그니처)
- 탭 4개: 버블차트(등락률×거래대금) / 히트맵(날짜×테마) / 테마추이(최대 5개, 누적 등락률 라인) / **기간분석**(viz_spec.md §대시보드 탭 참조)
- 기간 드롭다운 + 거래대금 슬라이더(0~5조)
- 하단 "Last updated / Max date" (/meta/health 연동)
- Parquet → FastAPI → fetch() 방식, 로딩 속도 우선

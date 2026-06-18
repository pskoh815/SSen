# Data Contract (E1)

## 파티션 규칙

| 테이블 | 파티션 방식 | 경로 예시 |
|--------|------------|---------|
| fact_daily_stock | 월 (yearmonth=YYYYMM) | `data/parquet/fact_daily_stock/yearmonth=202411/data.parquet` |
| fact_kospi | 월 (yearmonth=YYYYMM) | `data/parquet/fact_kospi/yearmonth=202411/data.parquet` |
| dim_theme | 비파티션 (최신 스냅샷) | `data/parquet/dim_theme/data.parquet` |
| relative_strength | 스냅샷 (파일명=날짜) | `data/parquet/snapshots/relative_strength/260529.parquet` |

## fact_daily_stock 스키마

| 컬럼 | 타입 | 원본 컬럼 | 비고 |
|------|------|----------|------|
| date | date32 | 날짜 | timezone-naive, 하루 단위 |
| rank | int32 | 순위 | |
| code | string | 종목코드 | 6자리 leading-zero 보존 |
| name | string | 종목명 | |
| market | string | 시장구분 | KOSPI/KOSDAQ |
| base_price | int64 | 시작일기준가 | nullable |
| close_price | int64 | 종료일종가 | nullable |
| change | int64 | 대비 | nullable |
| change_pct | float64 | 등락률 | |
| volume_sum | int64 | 거래량_합계 | nullable |
| volume_avg | int64 | 거래량_일평균 | nullable |
| amount_sum | int64 | 거래대금_합계 | nullable |
| amount_avg | int64 | 거래대금_일평균 | nullable |
| shares | int64 | 상장주식수 | nullable |
| vs_kospi_pct | float64 | 코스피 대비 등락률 | |
| mktcap | int64 | 시가총액 | nullable |
| amount_vs_mktcap_pct | float64 | 시총 대비 거래대금 증가율 | |
| size_class | string | 규모 | 대형주/중형주/소형주 |
| contrib_score | float64 | 기여점수 | |
| contrib_rank | int32 | 기여도순위 | |
| theme1 | string | 테마(1차) | nullable |
| theme1_rank | int32 | 테마(1차) 순위 | |
| theme1_amount | int64 | 테마(1차) 거래대금 | nullable |
| theme1_pct | float64 | 테마(1차) 등락률 | |
| theme2_rank | int32 | 테마(2차) 순위 | |
| theme2 | string | 테마(2차) | nullable |
| theme2_amount | int64 | 테마(2차) 거래대금 | nullable |
| theme2_pct | float64 | 테마(2차) 등락률 | |
| strength | string | 강약 판정 | ↑ 강세 / ↗ 약한강세 / ↓ 약세 등 |

**유니크 키**: (date, code, rank)

## fact_adr 스키마 (상승하락비율)

| 컬럼 | 타입 | 원본 컬럼 |
|------|------|----------|
| date | date32 | 날짜 |
| index_name | string | 지수 (KOSPI/KOSDAQ) |
| up_count | int32 | 상승종목수 |
| down_count | int32 | 하락종목수 |
| flat_count | int32 | 보합종목수 |
| adr | float64 | 하락 대비 상승비율 |

**유니크 키**: (date, index_name)  
**파티션**: 월 (yearmonth=YYYYMM) — `data/parquet/fact_adr/yearmonth=YYYYMM/data.parquet`

## fact_kospi 스키마

| 컬럼 | 타입 | 원본 컬럼 |
|------|------|----------|
| date | date32 | Date |
| open | float64 | Open |
| high | float64 | High |
| low | float64 | Low |
| close | float64 | Close |
| volume | int64 | Volume |
| change_rate | float64 | Increase rate |

**유니크 키**: (date)

## dim_theme 스키마

| 컬럼 | 타입 | 원본 컬럼 |
|------|------|----------|
| name | string | 종목명 |
| code | string | 종목코드 (6자리) |
| theme1 | string | 테마(1차) |
| theme2 | string | 테마(2차) |
| shares | int64 | 상장주식수 |

## Manifest 형식

`data/parquet/_manifest.json`:

```json
{
  "fact_daily_stock": {
    "202411": {
      "yearmonth": "202411",
      "rowcount": 100,
      "min_date": "2024-11-01",
      "max_date": "2024-11-30",
      "file_bytes": 12345,
      "checksum_md5": "abc123...",
      "written_at": "2026-06-04T00:00:00"
    }
  },
  "last_updated_at": "2026-06-04T00:00:00",
  "last_source_file": "SSen분석_최종260529.xlsx"
}
```

## Overlap 정책

| 정책 | CLI 옵션 | 동작 |
|------|---------|------|
| rebuild (기본) | `--overlap rebuild` | 겹치는 월 파티션 재생성 |
| new_only | `--overlap new_only` | 신규 월만 추가, 기존 월 건드리지 않음 |

## 룩어헤드 편향 방지

- 모든 신호는 t일 데이터 기준으로 산출, 진입은 t+1 종가 기준
- 파생테이블 계산 시 미래 날짜 데이터 참조 금지
- 백테스트 결과 리포트에 체결 기준 명시 필수

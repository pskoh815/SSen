# data.go.kr 수집 규약

서비스키: 코드/문서에 평문 저장 금지. `.env`의 `DATA_GO_KR_KEY`에서 읽기
(`.env`는 `.gitignore` 포함 필수).

```
주식시세: https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo
지수시세: https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getIndexPriceInfo
페이징:   numOfRows=1000 고정, totalCount 기준 페이지 계산
시장:     mrktCls = KOSPI / KOSDAQ
날짜:     beginBasDt / endBasDt (YYYYMMDD)
청크:     5일 단위 분할 요청 (서버 버그 회피)
```

수집 스크립트: `SSen_Money.py`(거래대금 A~M열) / `지수_상승종목수.py`(상승하락비율,
날짜당 KOSPI·KOSDAQ 2행) / `코스피지수_시세.py`(KospiPrice).

## Parquet upsert 패턴

```python
combined = pd.concat([existing, new_df]).drop_duplicates(subset=key_cols, keep='last')
```

## 자동화

`run_daily.bat` → 작업 스케줄러 매 영업일 16:30:
```batch
cd /d C:\MyClaude\ssen-dashboard
powershell -ExecutionPolicy Bypass -File run.ps1 update >> logs\update_%date:~0,4%%date:~5,2%%date:~8,2%.log 2>&1
```

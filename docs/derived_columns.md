# 파생 컬럼 스펙 (거래대금 시트 N~AC열)

구현: `src/ssen/derived/calc_derived.py`. A~M열은 API 수집 원본
(날짜, 순위, 종목코드, 종목명, 시장구분, 시작일기준가, 종료일종가, 대비, 등락률,
거래량_합계, 거래량_일평균, 거래대금_합계, 거래대금_일평균).

## N: 상장주식수
```python
df['상장주식수'] = df['종목코드'].map(themes.set_index('종목코드')['상장주식수']).fillna('')
```

## O: 코스피 대비 등락률
```python
kospi_rate = kospi.set_index('Date')['Increase rate']
df['코스피 대비 등락률'] = df['등락률'] - df['날짜'].map(kospi_rate).fillna(0)
```

## P: 시가총액
```python
df['시가총액'] = df['상장주식수'] * df['종료일종가']
```

## Q: 시총 대비 거래대금 증가율 (상한 500)
```python
df['시총 대비 거래대금 증가율'] = (df['거래대금_일평균'] / df['시가총액'] * 100).clip(upper=500)
```

## R: 규모
KOSPI: 시총 ≥6.5조 대형 / ≥7천억 중형 / 미만 소형.
KOSDAQ: ≥1조 대형 / ≥3천억 중형 / 미만 소형.

## S: 기여점수 (핵심 지표)
```python
score = (np.clip(등락률/30, -1, 1) * 0.5
       + np.clip(코스피대비등락률/38, -1, 1) * 0.3
       + np.clip(시총대비거래대금증가율/500, 0, 1) * np.sign(등락률) * 0.2) * 10
기여점수 = round(np.clip(score, -10, 10), 1)
```

## T: 기여도순위
날짜별 기여점수 내림차순 rank(method='min'), 100위 초과는 None.

## U/Z: 테마(1차/2차)
Themes 시트에서 종목코드 map. 빈값·0은 ''.

## V/Y: 테마 순위
`groupby(['날짜', 테마])['기여점수'].rank(ascending=False, method='min')`

## W/AA: 테마 거래대금
`groupby(['날짜', 테마])['거래대금_일평균'].transform('sum')`

## X/AB: 테마 등락률
`groupby(['날짜', 테마])['코스피 대비 등락률'].transform('mean')`

## AC: 강약 판정
기여점수 기준: ≥7 🔥강세주도 / ≥4 ↑강세 / ≥1 ↗약한강세 / ≥-1 →중립 /
≥-2 ↘약한약세 / ≥-7 ↓약세 / 미만 ❄️약세주도

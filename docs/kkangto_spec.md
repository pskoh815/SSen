# 깡토식 추세 추종 매매 시스템 v3 스펙

철학: 예측 금지(가격·거래량 기반) / 생존>수익(약세장 현금도 포지션) /
손실 기계적 -8%, 수익 추세 끝까지(손익비 1:3+) / 룩어헤드 절대 금지.

## 전역 파라미터 (v3 튜닝값)

```python
P = {
    # 기본 필터
    'MIN_CAP': 200_000_000_000, 'MIN_PRICE': 1000, 'MIN_VOL_20D': 100_000,
    # 장세 (KOSPI 0.7 + KOSDAQ 0.3 가중, MA60 기준)
    'MARKET_MA': 60, 'MARKET_WEIGHT_KOSPI': 0.7, 'MARKET_WEIGHT_KOSDAQ': 0.3,
    # RS (백분위, 미래참조 없음)
    'RS_W_12M': 0.40, 'RS_W_3M': 0.30, 'RS_W_6M': 0.20, 'RS_W_1M': 0.10,
    'RS_MIN': 85, 'RS_1M_MIN': 70,   # v3: 1M 85→70 완화
    # MTT
    'MTT_MIN_SCORE': 7, 'MTT_MA_SLOPE_PERIOD': 5,
    # 베이스
    'BASE_MAX_RETRACE': 0.50, 'BASE_MIN_DAYS': 15,
    'BASE_VOL_SHRINK_RATIO': 0.7,    # 10일 거래량 < 20일 × 0.7
    # 돌파
    'BREAKOUT_VOL_MULT': 1.5, 'BREAKOUT_LOOKBACK': 20, 'BREAKOUT_PRICE_BUFFER': 0.01,
    # 손절/트레일링
    'STOP_LOSS_BULL': 0.92, 'STOP_LOSS_BEAR': 0.95,
    'TRAIL_B_ENTRY': 0.16,    # +16% → 본절
    'TRAIL_1R_ENTRY': 0.24,   # +24% → 1/3익절 + entry×1.08 트레일
    # Adaptive Slope Exit
    'ROC20_FAST': 20.0, 'ROC20_MID': 8.0,
    'SELL_DROP_MIN': 3.0, 'SELL_VOL_LEN': 20, 'SELL_VOL_MULT': 1.5,
    # 클라이맥스
    'CLIMAX_VOL_MULT': 2.5, 'CLIMAX_TAIL_RATIO': 0.6, 'CLIMAX_PRICE_GAP': 0.07,
    # 타임컷
    'TIMECUT_DAYS': 10, 'TIMECUT_BAND': 0.03,
    # 유닛
    'RISK_PCT': 0.02, 'UNIT_MKT_BULL': 2, 'UNIT_MKT_BEAR': 1,
    'UNIT_STOCK_INIT': 1, 'UNIT_STOCK_MAX': 3,
}
```

## 지표 정의

**RS**: 기간별(20/60/120/250일) `pct_change`를 당일 기준 `rank(pct=True)*100` 백분위로
변환 후 가중합. shift 없음(당일 종가까지만 사용).

**MTT (9개 조건, 7점 이상 통과)**:
1. C > MA150 & C > MA200
2. MA150 > MA200
3. MA200 > MA200.shift(20)
4. MA50 > MA150 & MA50 > MA200
5. C > MA50
6. C ≥ 52주저점 × 1.30
7. C ≥ 52주고점 × 0.75
8. MA20/MA60/MA120 모두 5일 전 대비 상승
9. 20일 저점 3개(LL20 > LL20.shift(20) > LL20.shift(40)) 상승 (v3: 주봉 저점 대체)

**Adaptive Slope Exit**: ROC20 ≥20 → MA10 / ≥8 → MA20 / 미만 → MA60 대응.
- SELL_STRONG: MA 하향 크로스(전일 MA와 비교, 미래참조 방지) & 장대음봉(body ≥3%) & 거래량 ≥20일평균×1.5
- SELL_BASIC: C < MA & 장대음봉 & 거래량급증 (크로스 제외)
- CLIMAX: 거래량 ≥연평균×2.5 & 음봉 & 전일比 -7% 이상 | 윗꼬리 비율 ≥0.6

## 매수 스크리닝 6단계

1. 시총 ≥2천억 & 가격 ≥1,000원
2. 20일 평균 거래량 ≥10만주
3. RS_weighted ≥85 & 지수 RS 초과 & RS_1M ≥70
4. MTT ≥7
5. 베이스: 52주 저점 이후 조정률 <50% & 거래량 30%+ 감소 & 베이스 ≥15일
6. 돌파 트리거: 거래량 ×1.5 + 양봉 + 20일 고점 +1% 돌파 → 점수 3=🔥강력돌파 / 2=⚡임박 / 1·0=⏳형성중

## 보유 점검 (매도 우선순위)

🛑 손절선 → 🔴 SELL_STRONG → 🟡 SELL_BASIC → 🟠 클라이맥스 → 📉 50%반납(최고가-진입가의 50% 반납) → ⏰ 타임컷(10일 & ±3% 박스).
트레일링: +16% 본절 / +24% 1/3익절 + entry×1.08 고정.

## 출력

`매매결과_YYYYMMDD.xlsx`: 시장현황 / 매수후보(종목명·시총억·RS_w·RS_1M·MTT·ROC20·구간·돌파신호) / 보유점검 3개 시트.

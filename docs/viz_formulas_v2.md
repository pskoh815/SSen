# 시각화 시트 v2 수식 — dedup·복리 교정판

요구사항: Excel 365 (LET/LAMBDA/MAP/FILTER/UNIQUE/SORTBY/TAKE/VSTACK 사용).
적용 전 백업 권장. `$B$1`=시작일, `$B$2`=종료일 그대로 사용.

공통 원리 — **dedup**: `날짜&"|"&테마` 키로 UNIQUE를 만들어 (날짜×테마)당 1행만 남김
(기존 SUMIFS는 종목 행마다 중복 합산 → 최대 18.7배 부풀림).
**복리**: `PRODUCT(1+r/100)-1` (기존 단순 합산은 복리 대비 최대 3배 괴리).

---

## ① 상승 테마 TOP10 — H4 교체 (스필 H4:L14, 5열)

기존 H4 수식 삭제 후 입력. 기존 4열(H:K)에서 "상승일 비율"이 추가돼 5열(H:L)이 됩니다.
L4:L14가 비어 있어야 스필됩니다(원본 파일은 빈 스페이서라 충돌 없음).

```
=LET(
  sd, $B$1,  ed, $B$2,
  d, MostActiveStocks[날짜],
  t, MostActiveStocks[테마(1차)],
  r, MostActiveStocks[테마(1차) 등락률],
  v, MostActiveStocks[테마(1차) 거래대금],
  ok, (d>=sd)*(d<=ed)*(t<>""),
  fd, FILTER(d, ok),  ft, FILTER(t, ok),
  fr, FILTER(r, ok),  fv, FILTER(v, ok),
  key, fd & "|" & ft,
  ix, XMATCH(UNIQUE(key), key),
  dt, INDEX(ft, ix),  dr, INDEX(fr, ix),  dv, INDEX(fv, ix),
  uth, UNIQUE(dt),
  cumr, MAP(uth, LAMBDA(x, (PRODUCT(IF(dt=x, 1+dr/100, 1))-1)*100)),
  cumv, MAP(uth, LAMBDA(x, SUM((dt=x)*dv))),
  upr,  MAP(uth, LAMBDA(x, SUM((dt=x)*(dr>0)) / SUM(--(dt=x)))),
  lv, LN(1+cumv),
  nv, IF(MAX(lv)=MIN(lv), 0.5, (lv-MIN(lv))/(MAX(lv)-MIN(lv))),
  nr, IF(MAX(cumr)=MIN(cumr), 0.5, (cumr-MIN(cumr))/(MAX(cumr)-MIN(cumr))),
  score, nv*0.5 + nr*0.3 + upr*0.2,
  mat, HSTACK(uth, ROUND(cumr,1), cumv, ROUND(upr,2), ROUND(score,3)),
  VSTACK({"상승 테마","누적 상승율(복리)","누적 거래대금","상승일 비율","종합점수"},
         TAKE(SORTBY(mat, score, -1), 10))
)
```

교정 포인트: dedup [F1] + 복리 [F2] + 거래대금 LN 정규화 [F3] + 지속성(상승일 비율) 가중 0.2.
가중치: 거래대금 0.5 / 상승율 0.3 / 상승일비율 0.2 — 셀 분리해 튜닝 가능.

---

## ② 하락 테마 TOP10 — H17 교체 (스필 H17:K27, 4열)

```
=LET(
  sd, $B$1,  ed, $B$2,
  d, MostActiveStocks[날짜],
  t, MostActiveStocks[테마(1차)],
  r, MostActiveStocks[테마(1차) 등락률],
  v, MostActiveStocks[테마(1차) 거래대금],
  ok, (d>=sd)*(d<=ed)*(t<>""),
  fd, FILTER(d, ok),  ft, FILTER(t, ok),
  fr, FILTER(r, ok),  fv, FILTER(v, ok),
  key, fd & "|" & ft,
  ix, XMATCH(UNIQUE(key), key),
  dt, INDEX(ft, ix),  dr, INDEX(fr, ix),  dv, INDEX(fv, ix),
  uth, UNIQUE(dt),
  cumr, MAP(uth, LAMBDA(x, (PRODUCT(IF(dt=x, 1+dr/100, 1))-1)*100)),
  cumv, MAP(uth, LAMBDA(x, SUM((dt=x)*dv))),
  neg, cumr<0,
  IF(SUM(--neg)=0, "기간 내 하락 테마 없음",
  LET(
    nth, FILTER(uth, neg),
    nr0, FILTER(cumr, neg),
    nv0, FILTER(cumv, neg),
    mag, -nr0,
    nm, IF(MAX(mag)=MIN(mag), 0.5, (mag-MIN(mag))/(MAX(mag)-MIN(mag))),
    lv, LN(1+nv0),
    nv, IF(MAX(lv)=MIN(lv), 0.5, (lv-MIN(lv))/(MAX(lv)-MIN(lv))),
    dscore, nm*0.6 + nv*0.4,
    VSTACK({"하락 테마","누적 하락율(복리)","누적 거래대금","하락점수"},
           TAKE(SORTBY(HSTACK(nth, ROUND(nr0,1), nv0, ROUND(dscore,3)), dscore, -1), 10))
  ))
)
```

교정 포인트 [F4]: 기존 `상승율×거래대금` 곱 정렬(금융 -9.8%가 엔터 -26.9%보다 상위로 역전)
→ 하락 테마(누적상승율<0)만 모집단으로 **하락점수 = 정규화(하락폭)×0.6 + 정규화(LN 거래대금)×0.4**.

---

## ③ 부상 테마 TOP10 (신규) — H30 입력 (스필 H30:L40)

기간을 이등분해 후반 복리수익 − 전반 복리수익(로테이션)이 큰 테마. **추격 매수 방지 +
다음 주도 테마 포착**용 — 누적 1위라도 로테이션이 음수면 이미 식은 테마.

```
=LET(
  sd, $B$1,  ed, $B$2,  md, sd + (ed-sd)/2,
  d, MostActiveStocks[날짜],
  t, MostActiveStocks[테마(1차)],
  r, MostActiveStocks[테마(1차) 등락률],
  ok, (d>=sd)*(d<=ed)*(t<>""),
  fd, FILTER(d, ok),  ft, FILTER(t, ok),  fr, FILTER(r, ok),
  key, fd & "|" & ft,
  ix, XMATCH(UNIQUE(key), key),
  dd, INDEX(fd, ix),  dt, INDEX(ft, ix),  dr, INDEX(fr, ix),
  uth, UNIQUE(dt),
  front, MAP(uth, LAMBDA(x, (PRODUCT(IF((dt=x)*(dd<=md), 1+dr/100, 1))-1)*100)),
  back,  MAP(uth, LAMBDA(x, (PRODUCT(IF((dt=x)*(dd> md), 1+dr/100, 1))-1)*100)),
  rot, back - front,
  pos, rot > 0,
  IF(SUM(--pos)=0, "부상 테마 없음",
  VSTACK({"부상 테마","전반 수익(복리)","후반 수익(복리)","로테이션"},
         TAKE(SORTBY(HSTACK(FILTER(uth,pos), ROUND(FILTER(front,pos),1),
                            ROUND(FILTER(back,pos),1), ROUND(FILTER(rot,pos),1)),
                     FILTER(rot,pos), -1), 10)))
)
```

---

## ④ BunSeok 테이블 — AA열 신규 "복리 수익률" (AA4 헤더, AA5 수식 → 자동 채움)

```
=IFERROR(ROUND((PRODUCT(1 + FILTER(MostActiveStocks[등락률],
   (MostActiveStocks[날짜]>=$B$1)*(MostActiveStocks[날짜]<=$B$2)*
   (MostActiveStocks[종목코드]=[@종목코드]))/100)-1)*100, 1), "")
```

주의 2가지:
- 기존 Y열(코스피 대비 누적 상승율)의 합산은 그대로 두되, "코스피 대비" 일별 차이를
  복리화하는 건 수학적으로 무의미하므로 복리는 **원 등락률** 기준으로 별도 열 추가
- 이 값은 **거래대금 상위에 등장한 날만의 수익률**(평균 커버리지 ~18%) — 종목의 실제
  기간 수익률이 아님. 해석 시 `기여 횟수`(W열)와 함께 볼 것

---

## 적용 후 정리

- ①②가 MostActiveStocks에서 직접 계산하므로 **Themes1Hab 테이블(AB:AD)은 더 이상
  참조되지 않음** — 삭제 가능 (BunSeok은 A4 강세주도종목 수식이 참조하므로 유지)
- ①~③은 전체 데이터(157,200행)에서도 dedup 후 배열 연산이라 SUMIFS×120테마 반복보다
  오히려 가벼움. 단 자동 계산 지연 시 수식 → 계산 옵션 → 수동 + F9 권장

## 검증 기대값 (샘플 파일, 2026-02-06 ~ 2026-04-29)

| 블록 | 기대값 |
|---|---|
| ① 1위 | 통신 — 누적 1293.1% / 거래대금 36,945,180,062,678 / 상승일비율 0.76 / 종합 0.797 |
| ① 2위 | 반도체 — 288.1% / 718,477,749,661,953 / 0.82 / 0.736 |
| ② 1~3위 | 클라우드(하락점수 0.90) → 엔터(0.69) → 은행(0.56) — 금융·자동차 역전 해소 |
| ③ 1~3위 | 방산(+115.5 = 118.9−3.4) → 비철금속(+98.9) → 전력설비(+88.3) |
| ④ 대우건설 | 복리 +653.3% (기존 합산 방식이면 227.8%) |

`period_analysis.py`와 동일 로직이므로 값이 일치해야 정상 (부동소수점 오차 허용).

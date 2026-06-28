# -*- coding: utf-8 -*-
"""기간분석 v2 — 시각화 시트 수식의 논리 결함을 교정한 참조 구현.

교정 사항 (v1 = Excel 시각화 시트):
  [F1] 테마 집계: 종목 행 단위 SUMIFS(종목수 가중, 최대 18.7x 부풀림) → 날짜별 1회 dedup
  [F2] 누적 상승율: 단순 합산 → 복리 누적 (1+r).prod()-1
  [F3] 종합점수: min-max 정규화(outlier 압살) → 거래대금 log1p 후 정규화
  [F4] 하락 테마 정렬: 상승율×거래대금 곱(-3.5% 자동차가 -27% 엔터보다 상위로 역전)
       → 하락율 자체 + 거래대금 가중 종합점수로 분리
  [F5] 순위 모멘텀: 테마 내 절대 순위차(테마 규모 1~17종목으로 스케일 상이)
       → 백분위 순위 기반으로 정규화
  [F6] 누적 기여점수: 등장횟수 편향 → 평균 기여점수·등장일수·복합점수 분해
추가 인사이트:
  [I1] 표본 커버리지(등장일/거래일) 명시 — 거래대금 상위 미등장일 수익 누락 한계 표기
  [I2] 테마 로테이션: 기간 전/후반 점수 비교로 부상/소멸 테마 감지
  [I3] 주도주 진입 신호일: 첫 기여점수>=7 날짜 (이후 성과 추적용)
"""
import numpy as np
import pandas as pd


def _minmax(s: pd.Series) -> pd.Series:
    rng = s.max() - s.min()
    return (s - s.min()) / rng if rng > 0 else pd.Series(0.5, index=s.index)


def _compound(pct: pd.Series) -> float:
    return ((1 + pct / 100).prod() - 1) * 100


def theme_analysis(m: pd.DataFrame, top_n: int = 10) -> dict:
    """[F1~F4] 테마 기간 집계. m = 기간 필터된 거래대금 데이터."""
    daily = (m.groupby(['날짜', '테마(1차)'])
              .agg(등락률=('테마(1차) 등락률', 'first'),      # F1: 날짜별 1회
                   거래대금=('테마(1차) 거래대금', 'first'),
                   종목수=('종목코드', 'nunique'))
              .reset_index())
    g = daily.groupby('테마(1차)')
    t = pd.DataFrame({
        '누적상승율': g['등락률'].apply(_compound),            # F2: 복리
        '누적거래대금': g['거래대금'].sum(),
        '등장일수': g.size(),
        '상승일비율': g['등락률'].apply(lambda s: (s > 0).mean()),  # 지속성
        '일평균종목수': g['종목수'].mean(),
    })
    # F3: log 정규화 종합점수 (+ 지속성 반영)
    t['종합점수'] = (_minmax(np.log1p(t['누적거래대금'])) * 0.5
                  + _minmax(t['누적상승율']) * 0.3
                  + t['상승일비율'] * 0.2).round(3)
    # I2: 로테이션 — 전/후반 등락률 복리 비교
    mid = m['날짜'].min() + (m['날짜'].max() - m['날짜'].min()) / 2
    front = daily[daily['날짜'] <= mid].groupby('테마(1차)')['등락률'].apply(_compound)
    back = daily[daily['날짜'] > mid].groupby('테마(1차)')['등락률'].apply(_compound)
    t['전반수익'] = front.reindex(t.index).fillna(0).round(1)
    t['후반수익'] = back.reindex(t.index).fillna(0).round(1)
    t['로테이션'] = (t['후반수익'] - t['전반수익']).round(1)   # +: 부상, -: 소멸

    up = t.sort_values('종합점수', ascending=False).head(top_n)
    # F4: 하락은 누적상승율 음수 모집단에서 하락폭·거래대금으로 별도 점수
    neg = t[t['누적상승율'] < 0].copy()
    if not neg.empty:
        neg['하락점수'] = (_minmax(-neg['누적상승율']) * 0.6
                       + _minmax(np.log1p(neg['누적거래대금'])) * 0.4).round(3)
        down = neg.sort_values('하락점수', ascending=False).head(top_n)
    else:
        down = neg
    rising = t[t['로테이션'] > 0].sort_values('로테이션', ascending=False).head(top_n)
    return {'up': up, 'down': down, 'rotation': rising}


def stock_leaders(m: pd.DataFrame, total_days: int, top_n: int = 30,
                  min_days: int = 3) -> pd.DataFrame:
    """[F5,F6,I1,I3] 강세 주도종목. 조건은 v1 유지(max>=7 & 최근3회 평균>=3 & 모멘텀>0),
    지표 정의만 교정."""
    m = m.sort_values('날짜')
    # F5: 테마 내 백분위 순위 (1=테마 최상위, 0=최하위)
    m = m.assign(순위백분위=1 - (m['테마(1차) 순위'] - 1)
                 / m.groupby(['날짜', '테마(1차)'])['종목코드'].transform('count').clip(lower=1))
    rows = []
    for (code, name), g in m.groupby(['종목코드', '종목명']):
        if len(g) < min_days:
            continue
        sc, pr = g['기여점수'], g['순위백분위']
        recent3 = sc.tail(3).mean()
        if not (sc.max() >= 7 and recent3 >= 3):
            continue
        momentum = pr.tail(3).mean() - pr.iloc[0]          # F5: 백분위 차 (+: 상승)
        if momentum <= 0:
            continue
        first_lead = g.loc[sc >= 7, '날짜']
        rows.append({
            '종목코드': code, '종목명': name,
            '테마(1차)': g['테마(1차)'].iloc[-1],
            '순위모멘텀': round(momentum, 3),
            '평균기여점수': round(sc.mean(), 2),            # F6: 평균·횟수 분해
            '등장일수': len(g),
            '커버리지': round(len(g) / total_days, 2),      # I1
            '복리수익률': round(_compound(g['등락률']), 1),  # F2 (등장일 한정, 커버리지 참조)
            '코스피대비강세횟수': int((g['코스피 대비 등락률'] > 0).sum()),
            '누적거래대금': g['거래대금_일평균'].sum(),
            '첫주도신호일': first_lead.iloc[0].date() if len(first_lead) else None,  # I3
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out['복합점수'] = (out['평균기여점수'] / 10
                    * np.log1p(out['등장일수'])
                    * (1 + out['순위모멘텀'])).round(3)      # F6
    return out.sort_values('복합점수', ascending=False).head(top_n).reset_index(drop=True)


def breadth(m: pd.DataFrame) -> pd.DataFrame:
    """강세 분포 (v1 기준 유지: >=4 주도 / 1~4 강세 / -1~1 중립 / <-1 약세)."""
    def dist(g):
        sc = g['기여점수'].dropna()
        lead, strong = (sc >= 4).sum(), ((sc >= 1) & (sc < 4)).sum()
        neu, weak = ((sc >= -1) & (sc < 1)).sum(), (sc < -1).sum()
        n = len(sc)
        return pd.Series({'강세주도': lead, '강세': strong, '중립': neu, '약세': weak,
                          '강세비율': round((lead + strong) / n, 4) if n else 0})
    by_size = m.groupby('규모').apply(dist, include_groups=False)
    by_mkt = m.groupby('시장구분').apply(dist, include_groups=False)
    return pd.concat({'규모': by_size, '시장': by_mkt})


def run(df: pd.DataFrame, start: str, end: str) -> dict:
    df = df.copy()
    df['날짜'] = pd.to_datetime(df['날짜'])
    m = df[(df['날짜'] >= start) & (df['날짜'] <= end)]
    total_days = m['날짜'].nunique()
    return {
        'themes': theme_analysis(m),
        'leaders': stock_leaders(m, total_days),
        'breadth': breadth(m),
        'meta': {'start': start, 'end': end, '거래일': total_days,
                 '종목수': m['종목코드'].nunique()},
    }


def theme_trend(m: pd.DataFrame, top: int = 20) -> dict:
    """[§1-T] 테마 추이 — /api/period/theme-trend 응답 생성.

    m = 기간 필터된 거래대금(MostActiveStocks) 행.
    필요 컬럼: 날짜, 종목코드, 테마(1차), 테마(1차) 등락률, 테마(1차) 거래대금.
    반환: {"dates": [...], "series": [{"theme1": str, "cum": [...]}]}
      - dedup: (날짜×테마) 1회 [F1] / 누적: 복리 [F2]
      - 미등장일은 직전 누적값 유지(carry-forward, 등락 0% 처리) — null 없음
      - series 정렬 = 종합점수 내림차순 (theme_analysis와 동일 점수)
    """
    m = m.copy()
    m['날짜'] = pd.to_datetime(m['날짜'])
    # 종합점수 상위 top개 테마 (theme_analysis 점수 재사용)
    scores = theme_analysis(m, top_n=10**6)['up']
    top_themes = list(scores.index[:top])

    daily = (m[m['테마(1차)'].isin(top_themes)]
             .groupby(['날짜', '테마(1차)'])['테마(1차) 등락률'].first()
             .reset_index())
    dates = sorted(m['날짜'].unique())
    piv = (daily.pivot(index='날짜', columns='테마(1차)', values='테마(1차) 등락률')
                .reindex(dates))
    cum = ((1 + piv.fillna(0) / 100).cumprod() - 1) * 100  # fillna(0)=carry-forward

    return {
        'dates': [pd.Timestamp(d).strftime('%Y-%m-%d') for d in dates],
        'series': [
            {'theme1': t, 'cum': [round(float(v), 2) for v in cum[t]]}
            for t in top_themes if t in cum.columns
        ],
    }


# ═════════════════════════════════════════════════════════════════════════════
# DuckDB 기반 API 구현 (fact_daily_stock parquet, 영문 컬럼)
#   §1 /api/period/themes       : 테마별 복리수익·거래대금·종합점수·로테이션
#   §1-T /api/period/theme-trend: 상위 테마 누적수익률 시계열 (theme_trend 이식)
#   §2 /api/period/leaders      : 강세 주도종목 (순위모멘텀·복합점수·커버리지)
#   §3 /api/period/breadth      : 강세 분포 (기여점수 버킷 × 규모·시장)
#   §4 /api/period/leader-events: 기여점수 ≥7 이벤트 타임라인
# ═════════════════════════════════════════════════════════════════════════════

import math
from datetime import date
from pathlib import Path
from typing import Any, Optional

import duckdb

from .perf_timer import timed_db_query

_ROOT      = Path(__file__).resolve().parents[3]
_FDS_2020P = str(_ROOT / "data" / "parquet" / "fact_daily_stock" / "**" / "*.parquet").replace("\\", "/")
# 2015~2019 구간(2026-06-23 KRX OPEN API 백필 + calc_derived.py 동일 공식 재적용) — 압도적
# 테마종목/강세주도종목/이벤트 등 fact_daily_stock 의존 위젯을 2020년 이전에도 지원하기
# 위해 도입. fact_daily_stock과 dtype까지 동일한 스키마로 저장해 동일 glob 패턴으로 합쳐 읽음.
_FDS_PRE2020 = str(_ROOT / "data" / "parquet" / "fact_daily_stock_pre2020" / "**" / "*.parquet").replace("\\", "/")
_FDS       = "['" + _FDS_PRE2020 + "', '" + _FDS_2020P + "']"
_FKOSPI    = str(_ROOT / "data" / "parquet" / "fact_kospi"       / "**" / "*.parquet").replace("\\", "/")
_FOHLCV    = str(_ROOT / "data" / "market"  / "ohlcv"            / "**" / "*.parquet").replace("\\", "/")
_DIM_THEME = str(_ROOT / "data" / "parquet" / "dim_theme" / "data.parquet").replace("\\", "/")

MIN_APPEAR_DAYS = 2  # 기간수익률(시작가→종료가) 산출에 필요한 최소 등장일수

# fact_daily_stock(거래대금/테마 거래대금 등)은 2020-01-02부터만 존재 — 그 이전 구간은
# market_ohlcv(2015~2019 백필분)+dim_theme(현재 테마 매핑)을 조인해 테마분석을 대체
# 수행한다(2026-06-20 도입). 이 경로로 계산된 결과는 PRE2020_NOTICE를 응답에 포함.
FACT_DAILY_STOCK_MIN_DATE = date(2020, 1, 2)

PRE2020_NOTICE = [
    "2020년 이전 테마 분류는 현재 분류를 소급 적용한 것 (특히 AI/로봇 등 신생 테마는 "
    "당시 실제 인식과 다를 수 있음)",
    "2020년 이전 백테스트는 현재까지 거래 중인 종목 기준이며, 당시 거래대금 상위였으나 "
    "이후 상장폐지/합병된 종목은 포함되지 않음 (서바이버십 편향) — 실제 성과보다 "
    "낙관적일 수 있음",
]


def _ym(d: date) -> int:
    return d.year * 100 + d.month


def _load(start: date, end: date, cols: list[str]) -> pd.DataFrame:
    """DuckDB predicate pushdown — yearmonth 파티션 + 날짜 필터."""
    select = ", ".join(cols)
    sql = f"""
        SELECT {select}
        FROM   read_parquet({_FDS}, hive_partitioning=true)
        WHERE  yearmonth BETWEEN {_ym(start)} AND {_ym(end)}
          AND  date      BETWEEN '{start}' AND '{end}'
    """
    con = duckdb.connect()
    try:
        with timed_db_query():
            df = con.execute(sql).fetchdf()
    finally:
        con.close()
    return df


def _cumret(pct: pd.Series) -> float:
    """복리 누적 수익률 (%) [F2]."""
    return float(np.prod(1 + pct.fillna(0) / 100) - 1) * 100


def _norm01(s: pd.Series) -> pd.Series:
    """0-1 min-max 정규화 [F3]."""
    mn, mx = s.min(), s.max()
    return (s - mn) / max(float(mx - mn), 1e-9)


def _clean(records: list[dict]) -> list[dict]:
    """NaN/inf → None 직렬화 안전 변환."""
    return [
        {k: (None if (v is not None and isinstance(v, float) and not math.isfinite(v)) else v)
         for k, v in r.items()}
        for r in records
    ]


def _load_kospi(start: date, end: date) -> pd.DataFrame:
    sql = f"""
        SELECT date, close
        FROM   read_parquet('{_FKOSPI}', hive_partitioning=true)
        WHERE  yearmonth BETWEEN {_ym(start)} AND {_ym(end)}
          AND  date      BETWEEN '{start}' AND '{end}'
        ORDER BY date
    """
    con = duckdb.connect()
    try:
        with timed_db_query():
            df = con.execute(sql).fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"])
    return df


def _kospi_cum_series(start: date, end: date, dates: list) -> pd.Series:
    """코스피 종가 기준, dates[0] 종가 대비 각 날짜 종가의 누적수익률(%)."""
    kospi = _load_kospi(start, end)
    if kospi.empty:
        return pd.Series(0.0, index=dates)
    s = kospi.set_index("date")["close"].reindex(dates).ffill().bfill()
    return (s / s.iloc[0] - 1) * 100


def _kospi_change_rate(start: date, end: date) -> pd.DataFrame:
    """코스피 일별 등락률(change_rate, 전일 종가 대비 %) — 1거래일 폴백 전용."""
    sql = f"""
        SELECT date, change_rate
        FROM   read_parquet('{_FKOSPI}', hive_partitioning=true)
        WHERE  yearmonth BETWEEN {_ym(start)} AND {_ym(end)}
          AND  date      BETWEEN '{start}' AND '{end}'
        ORDER BY date
    """
    con = duckdb.connect()
    try:
        with timed_db_query():
            df = con.execute(sql).fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"])
    return df


def _single_trading_day_theme_stats(start: date, end: date, dates: list) -> pd.DataFrame:
    """1거래일 폴백 — 조회 범위 내 유효 등장종목(>=MIN_APPEAR_DAYS) 표본이 0개일 때
    (start==end뿐 아니라, 나머지 날짜가 전부 휴장일이라 실거래일이 1일뿐인 경우도 포함)
    사용. 시작가→종료가 비교가 정의 불가하므로, dominant_days와 동일한 theme1_pct
    (테마(1차) 당일 등락률)를 cumret_pct로 사용한다.
    appear_days=1, cumret_first/second(전반/후반)·rotation은 거래일 1개로는 정의
    불가 → None 처리(프론트는 null을 '−'로 표시).
    """
    if not dates:
        return pd.DataFrame()
    last_date = pd.Timestamp(dates[-1])

    theme_df = _load(start, end, ["date", "theme1", "theme1_amount", "theme1_pct"])
    theme_df = theme_df.dropna(subset=["theme1"])
    theme_df["date"] = pd.to_datetime(theme_df["date"])
    day = theme_df[theme_df["date"] == last_date]
    if day.empty:
        return pd.DataFrame()
    daily = day.groupby("theme1", as_index=False).agg(
        theme1_amount=("theme1_amount", "max"),
        theme1_pct=("theme1_pct", "max"),
    )

    stock_df = _load(start, end, ["date", "code", "name", "theme1", "change_pct"])
    stock_df = stock_df.dropna(subset=["theme1"])
    stock_df["date"] = pd.to_datetime(stock_df["date"])
    stock_day = stock_df[stock_df["date"] == last_date]
    up_ratio = stock_day.groupby("theme1")["change_pct"].apply(lambda s: float((s > 0).mean()))
    name_map = stock_day.dropna(subset=["name"]).groupby("code")["name"].last()

    jump_pct = (PRICE_JUMP_RATIO - 1) * 100  # 등락률 기준 ±300%(PRICE_JUMP_RATIO=4.0) 가드

    top_codes, top_names, top_rets, top_stocks_col = [], [], [], []
    for theme, day_pct in zip(daily["theme1"], daily["theme1_pct"]):
        stock_final = stock_day.loc[stock_day["theme1"] == theme].set_index("code")["change_pct"].dropna()
        stock_final = stock_final[stock_final.abs() < jump_pct]
        top3 = _pick_top_stocks(stock_final, day_pct, n=3)
        top_codes.append(top3[0][0] if top3 else None)
        top_names.append(name_map.get(top3[0][0]) if top3 else None)
        top_rets.append(round(top3[0][1], 2) if top3 else None)
        top_stocks_col.append([
            {"code": c, "name": name_map.get(c), "cumret_pct": round(r, 2)} for c, r in top3
        ])

    return pd.DataFrame(dict(
        theme1        = daily["theme1"],
        appear_days   = 1,
        cumret_pct    = daily["theme1_pct"].round(2),
        cumret_first  = None,
        cumret_second = None,
        total_amount  = daily["theme1_amount"].round(0),
        up_ratio      = daily["theme1"].map(up_ratio).fillna(0.0).round(3),
        rotation      = None,
        top_stock_code        = top_codes,
        top_stock_name        = top_names,
        top_stock_cumret_pct  = top_rets,
        top_stocks            = top_stocks_col,
    ))


PRICE_JUMP_RATIO = 4.0  # ±300% 등락률 가드: 전일 대비 4배 이상 상승 또는 1/4 이하로 하락
# (병합/분할 등 수정주가 미반영 단절 의심 — fact_daily_stock은 거래대금 TR(당일 스냅샷)
# 기반이라 수정주가 개념이 없어 원시 가격이 그대로 들어간다. 2026-06-19 발견: 신성이엔지
# (011930) 10:1 액면병합 신주상장으로 04-23 3,995원→05-15 39,950원 단절 — 트리맵 '테마 내
# 1위 종목'에 +1550% 비현실적 수익률로 노출됐었음. 근본 해결(과거 병합/분할 비율 소급
# 조정)은 전체 데이터 검증 부담이 커서, 우선 이 가드로 노출만 차단한다.)


def _stock_cum_frame(start: date, end: date) -> tuple[pd.DataFrame, pd.Series, list, pd.Series, set]:
    """종목별 일별 누적수익률(첫 등장일 종가 대비, %) 행렬 + 종목→테마(1차)/종목명 매핑
    + 가격단절(병합/분할 추정) 종목 코드 집합.

    - 등장하지 않은 날은 직전 종가를 carry-forward
    - 등장일수 < MIN_APPEAR_DAYS 종목은 시작가/종료가 비교가 불가하므로 제외
    """
    df = _load(start, end, ["date", "code", "name", "theme1", "close_price"])
    df["date"] = pd.to_datetime(df["date"])
    dates = sorted(df["date"].unique())
    if not dates:
        return pd.DataFrame(), pd.Series(dtype=object), dates, pd.Series(dtype=object), set()

    counts = df.groupby("code")["date"].nunique()
    valid_codes = counts[counts >= MIN_APPEAR_DAYS].index
    df = df[df["code"].isin(valid_codes)]

    theme_map = (
        df[df["theme1"].notna() & (df["theme1"] != "")]
        .groupby("code")["theme1"].agg(lambda s: s.mode().iloc[0])
    )
    name_map = df.dropna(subset=["name"]).groupby("code")["name"].last()

    piv  = df.pivot_table(index="date", columns="code", values="close_price", aggfunc="last").reindex(dates)
    piv  = piv.ffill()
    base = piv.bfill().iloc[0]
    cum  = (piv.div(base) - 1) * 100  # 첫 등장일 이전은 NaN

    day_ratio = piv / piv.shift(1)
    jump_mask = (day_ratio >= PRICE_JUMP_RATIO) | (day_ratio <= 1 / PRICE_JUMP_RATIO)
    jump_codes = set(jump_mask.columns[jump_mask.any()])

    return cum, theme_map, dates, name_map, jump_codes


def _load_dim_theme() -> pd.DataFrame:
    """code -> theme1/name 현재 매핑 (2020년 이전 구간 소급 적용용)."""
    return pd.read_parquet(_DIM_THEME, columns=["code", "name", "theme1"])


def _stock_cum_frame_ohlcv(start: date, end: date) -> tuple[pd.DataFrame, pd.Series, list, pd.Series, set]:
    """`_stock_cum_frame()`의 2020년 이전 전용 대체본 — fact_daily_stock 대신
    market_ohlcv(2015~2019 백필분) 종가 + dim_theme(현재 테마 매핑)을 종목코드로
    조인해 동일한 (cum, theme_map, dates, name_map, jump_codes) 형태를 만든다.
    테마 자체를 새로 분류하지 않고 현재 매핑을 그대로 재사용한다(사용자 지시).
    """
    sql = f"""
        SELECT date, code, close
        FROM   read_parquet('{_FOHLCV}', hive_partitioning=true)
        WHERE  date BETWEEN '{start}' AND '{end}'
    """
    con = duckdb.connect()
    try:
        with timed_db_query():
            df = con.execute(sql).fetchdf()
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"])
    dates = sorted(df["date"].unique())
    if not dates:
        return pd.DataFrame(), pd.Series(dtype=object), dates, pd.Series(dtype=object), set()

    dim = _load_dim_theme()
    df = df.merge(dim, on="code", how="inner")

    counts = df.groupby("code")["date"].nunique()
    valid_codes = counts[counts >= MIN_APPEAR_DAYS].index
    df = df[df["code"].isin(valid_codes)]
    if df.empty:
        return pd.DataFrame(), pd.Series(dtype=object), dates, pd.Series(dtype=object), set()

    theme_map = df.groupby("code")["theme1"].agg(lambda s: s.mode().iloc[0])
    name_map  = df.dropna(subset=["name"]).groupby("code")["name"].last()

    piv  = df.pivot_table(index="date", columns="code", values="close", aggfunc="last").reindex(dates)
    piv  = piv.ffill()
    base = piv.bfill().iloc[0]
    cum  = (piv.div(base) - 1) * 100

    day_ratio = piv / piv.shift(1)
    jump_mask = (day_ratio >= PRICE_JUMP_RATIO) | (day_ratio <= 1 / PRICE_JUMP_RATIO)
    jump_codes = set(jump_mask.columns[jump_mask.any()])

    return cum, theme_map, dates, name_map, jump_codes


def _theme_amount_from_ohlcv(start: date, end: date) -> tuple[pd.Series, pd.Series]:
    """`api_period_themes()`의 거래대금/등장일수 — 2020년 이전 전용. market_ohlcv의
    종목별 일별 amount(거래대금)를 dim_theme의 theme1으로 묶어 합산한다."""
    sql = f"""
        SELECT date, code, amount
        FROM   read_parquet('{_FOHLCV}', hive_partitioning=true)
        WHERE  date BETWEEN '{start}' AND '{end}'
    """
    con = duckdb.connect()
    try:
        with timed_db_query():
            df = con.execute(sql).fetchdf()
    finally:
        con.close()
    if df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=int)

    dim = _load_dim_theme()
    df = df.merge(dim[["code", "theme1"]], on="code", how="inner")
    daily_amt = df.groupby(["date", "theme1"], as_index=False)["amount"].sum()
    total_amount = daily_amt.groupby("theme1")["amount"].sum()
    appear_days  = daily_amt.groupby("theme1")["date"].nunique()
    return total_amount, appear_days


def _theme_cum_series(cum: pd.DataFrame, theme_map: pd.Series) -> pd.DataFrame:
    """종목별 누적수익률을 같은 테마(1차) 종목끼리 단순평균 → 테마 누적수익률 시계열.

    종목당 1표(시가총액 가중 아님) — 사용자 요청 산식:
    (종목1 기간수익률 + 종목2 기간수익률 + ... ) / 테마 종목수
    """
    out = {}
    for theme in sorted(theme_map.unique()):
        codes = [c for c in theme_map.index[theme_map == theme] if c in cum.columns]
        if not codes:
            continue
        out[theme] = cum[codes].mean(axis=1, skipna=True)
    if not out:
        return pd.DataFrame(index=cum.index)
    return pd.DataFrame(out).fillna(0.0)


def api_period_themes(start: date, end: date) -> dict[str, Any]:
    """§1 테마 분석.

    누적상승율 = 테마에 속한 각 종목의 (기간 시작일 종가 대비 종료일 종가) 등락률을
    단순평균한 값. cumret_first/second는 같은 기준을 전반부/후반부로 나눠 계산.
    """
    use_ohlcv   = end < FACT_DAILY_STOCK_MIN_DATE     # 데이터 소스 라우팅 (전체 기간이 2020년 이전일 때만)
    show_notice = start < FACT_DAILY_STOCK_MIN_DATE   # 안내문구 노출 (2020년 이전이 일부라도 포함되면)
    if use_ohlcv:
        cum, theme_map, dates, name_map, jump_codes = _stock_cum_frame_ohlcv(start, end)
    else:
        cum, theme_map, dates, name_map, jump_codes = _stock_cum_frame(start, end)
    if cum.empty or theme_map.empty:
        if use_ohlcv:
            return dict(rising=[], falling=[], rotating=[], all=[], notice=PRE2020_NOTICE)
        r = _single_trading_day_theme_stats(start, end, dates)
        result = _assemble_period_themes(r, rotating_supported=False)
        if show_notice:
            result["notice"] = PRE2020_NOTICE
        return result

    theme_cum = _theme_cum_series(cum, theme_map)
    if theme_cum.empty:
        result = dict(rising=[], falling=[], rotating=[], all=[])
        if show_notice:
            result["notice"] = PRE2020_NOTICE
        return result

    final = theme_cum.iloc[-1]
    mid_i = len(dates) // 2 - 1 if len(dates) >= 2 else 0
    mid   = theme_cum.iloc[mid_i]

    if use_ohlcv:
        total_amount, appear_days = _theme_amount_from_ohlcv(start, end)
    else:
        # 거래대금/등장일수: 기존 (date, theme1) dedup 합산 방식 유지
        amt_df = _load(start, end, ["date", "theme1", "theme1_amount"])
        amt_df = amt_df.dropna(subset=["theme1"])
        daily_amt = amt_df.groupby(["date", "theme1"], as_index=False)["theme1_amount"].max()
        total_amount = daily_amt.groupby("theme1")["theme1_amount"].sum()
        appear_days  = daily_amt.groupby("theme1")["date"].nunique()

    rows = []
    for theme in theme_cum.columns:
        codes = [c for c in theme_map.index[theme_map == theme] if c in cum.columns]
        cr = float(final[theme])
        cf = float(mid[theme])
        cs = ((1 + cr / 100) / (1 + cf / 100) - 1) * 100 if (1 + cf / 100) != 0 else 0.0
        stock_final = cum[codes].iloc[-1].dropna() if codes else pd.Series(dtype=float)
        up = float((stock_final > 0).mean()) if not stock_final.empty else 0.0
        top_candidates = stock_final.drop(labels=[c for c in jump_codes if c in stock_final.index])
        top3 = _pick_top_stocks(top_candidates, cr, n=3)
        top_code, top_ret = (top3[0] if top3 else (None, None))
        rows.append(dict(
            theme1=theme,
            appear_days=int(appear_days.get(theme, 0)),
            cumret_pct=round(cr, 2), cumret_first=round(cf, 2), cumret_second=round(cs, 2),
            total_amount=round(float(total_amount.get(theme, 0.0)), 0),
            up_ratio=round(up, 3), rotation=round(cs - cf, 2),
            top_stock_code=top_code,
            top_stock_name=name_map.get(top_code) if top_code else None,
            top_stock_cumret_pct=round(top_ret, 2) if top_ret is not None else None,
            top_stocks=[
                {"code": c, "name": name_map.get(c), "cumret_pct": round(r, 2)} for c, r in top3
            ],
        ))

    result = _assemble_period_themes(pd.DataFrame(rows), rotating_supported=True)
    if show_notice:
        result["notice"] = PRE2020_NOTICE
    return result


def _assemble_period_themes(r: pd.DataFrame, rotating_supported: bool) -> dict[str, Any]:
    """composite/fall_score 산출 + TOP10 정렬 — 정상 경로/1거래일 폴백 공통.

    rotating_supported=False면 rotation(전반↔후반 변화)이 정의되지 않는 폴백
    케이스이므로 rotating은 항상 빈 배열로 반환한다.
    """
    if r.empty:
        return dict(rising=[], falling=[], rotating=[], all=[])

    r["log_amt"]   = np.log1p(r["total_amount"].clip(lower=0))
    r["composite"] = (_norm01(r["log_amt"]) * 0.5          # [F3]
                      + _norm01(r["cumret_pct"]) * 0.3
                      + r["up_ratio"] * 0.2).round(4)

    r["fall_score"] = np.nan
    fm = r["cumret_pct"] < 0
    if fm.any():
        sub = r.loc[fm]
        r.loc[fm, "fall_score"] = (                        # [F4]
            _norm01(-sub["cumret_pct"])                       * 0.6
            + _norm01(np.log1p(sub["total_amount"].clip(lower=0))) * 0.4
        ).round(4)

    keep = ["theme1", "appear_days", "cumret_pct", "cumret_first",
            "cumret_second", "total_amount", "up_ratio",
            "composite", "rotation", "fall_score",
            "top_stock_code", "top_stock_name", "top_stock_cumret_pct", "top_stocks"]
    keep = [c for c in keep if c in r.columns]

    def top(df_: pd.DataFrame, col: str, n: int = 10, smallest: bool = False) -> list[dict]:
        df_ = df_.nsmallest(n, col) if smallest else df_.nlargest(n, col)
        return _clean(df_[keep].replace({np.nan: None}).to_dict("records"))

    return dict(
        rising   = top(r, "cumret_pct"),
        falling  = top(r[fm], "cumret_pct", 10, smallest=True) if fm.any() else [],
        rotating = top(r, "rotation") if rotating_supported else [],
        all      = _clean(r[keep].replace({np.nan: None}).to_dict("records")),
    )


def _pick_top_stock(stock_final: pd.Series, theme_cumret_pct: float) -> tuple[Optional[str], Optional[float]]:
    """테마 내 '1위 종목' 선정 — 테마 자체가 상승(cumret_pct>=0)이면 최고 수익률 종목,
    하락이면 최저(가장 많이 떨어진) 종목 1개. 압도적 테마 종목/강세주도 이벤트의
    까다로운 조건(3지표 동시 1위, 기여점수≥7)과 무관하게 항상 존재하는 값이어야
    트리맵 타일(테마가 있으면 항상 그려짐) 호버 정보가 빈틈없이 채워진다."""
    top3 = _pick_top_stocks(stock_final, theme_cumret_pct, n=1)
    if not top3:
        return None, None
    return top3[0]


def _pick_top_stocks(stock_final: pd.Series, theme_cumret_pct: float, n: int = 3) -> list[tuple[str, float]]:
    """테마 내 상위(또는 하락 테마면 최하위) 종목 N개 선정 — _pick_top_stock과 동일한
    상승/하락 기준이되, 트리맵 호버 카드에 1~3위를 함께 보여주기 위해 N개를 반환한다."""
    if stock_final.empty:
        return []
    ordered = stock_final.sort_values(ascending=theme_cumret_pct < 0)
    return [(code, float(ret)) for code, ret in ordered.head(n).items()]


def api_period_theme_trend(start: date, end: date, top: int = 20) -> dict[str, Any]:
    """§1-T 테마 추이 — 상위 top개 테마의 기간별 누적수익률 시계열 + 코스피 베이스라인.

    누적수익률(cum) = 테마에 속한 각 종목의, 기간 시작일(또는 첫 등장일) 종가 대비
    해당일 종가의 등락률을 종목수로 단순평균한 값.
    kospi = 코스피지수 종가의 시작일 대비 누적수익률(%) — 같은 기간 내 비교 기준선.
    정렬 = 누적상승율(cumret_pct) 내림차순 (api_period_themes와 동일 기준).
    """
    use_ohlcv   = end < FACT_DAILY_STOCK_MIN_DATE
    show_notice = start < FACT_DAILY_STOCK_MIN_DATE
    if use_ohlcv:
        cum, theme_map, dates, _name_map, _jump_codes = _stock_cum_frame_ohlcv(start, end)
    else:
        cum, theme_map, dates, _name_map, _jump_codes = _stock_cum_frame(start, end)
    if cum.empty or theme_map.empty:
        if use_ohlcv:
            return dict(dates=[], series=[], kospi=[], notice=PRE2020_NOTICE)
        result = _theme_trend_single_day_fallback(start, end, dates, top)
        if show_notice:
            result["notice"] = PRE2020_NOTICE
        return result

    theme_cum = _theme_cum_series(cum, theme_map)
    if theme_cum.empty:
        result = dict(dates=[], series=[], kospi=[])
        if show_notice:
            result["notice"] = PRE2020_NOTICE
        return result

    final = theme_cum.iloc[-1]
    top_themes = list(final.nlargest(top).index)

    # 코스피 베이스라인: fact_kospi 2014-07-25~ 백필 완료(2026-06-21)로 2015년 이후는
    # 정상 조회 가능 — 소스 분기 없이 항상 같은 함수 사용
    kospi_cum = _kospi_cum_series(start, end, dates)

    series = [
        dict(theme1=t, cum=[round(float(v), 2) for v in theme_cum[t]])
        for t in top_themes
    ]
    result = dict(
        dates =[pd.Timestamp(d).strftime("%Y-%m-%d") for d in dates],
        series=series,
        kospi =[round(float(v), 2) for v in kospi_cum],
    )
    if show_notice:
        result["notice"] = PRE2020_NOTICE
    return result


def _theme_trend_single_day_fallback(start: date, end: date, dates: list, top: int) -> dict[str, Any]:
    """api_period_themes의 1거래일 폴백과 동일 조건. 거래일이 1개뿐이라 '추이'는
    단일 포인트만 존재 — series의 cum은 그 날의 theme1_pct(당일 등락률) 1개값,
    kospi도 같은 날 change_rate(전일 종가 대비 %) 1개값을 사용한다.
    """
    if not dates:
        return dict(dates=[], series=[], kospi=[])
    last_date = pd.Timestamp(dates[-1])

    theme_df = _load(start, end, ["date", "theme1", "theme1_pct"])
    theme_df = theme_df.dropna(subset=["theme1"])
    theme_df["date"] = pd.to_datetime(theme_df["date"])
    day = theme_df[theme_df["date"] == last_date]
    if day.empty:
        return dict(dates=[], series=[], kospi=[])

    daily = day.groupby("theme1", as_index=False)["theme1_pct"].max()
    daily = daily.nlargest(top, "theme1_pct")

    kospi_df = _kospi_change_rate(start, end)
    kospi_today = kospi_df.loc[kospi_df["date"] == last_date, "change_rate"]
    kospi_val = float(kospi_today.iloc[0]) if not kospi_today.empty else 0.0

    return dict(
        dates =[last_date.strftime("%Y-%m-%d")],
        series=[dict(theme1=row.theme1, cum=[round(float(row.theme1_pct), 2)])
                for row in daily.itertuples()],
        kospi =[round(kospi_val, 2)],
    )


DOMINANT_MIN_STOCKS = 5  # 압도적 주도 테마 판정에 필요한 최소 종목수(테마 내 종목수가 이보다 적은 테마는 비교 대상에서 제외)


def api_period_dominant_days(start: date, end: date,
                              min_n_stocks: int = DOMINANT_MIN_STOCKS) -> dict[str, Any]:
    """일별 '압도적 주도 테마' 탐지.

    종목수 >= min_n_stocks인 테마들 중, 같은 날 등락률·종목수·거래대금
    3개 지표 모두 1위인 테마가 있으면 그 날을 주도일로 표시.
    """
    df = _load(start, end, ["date", "code", "theme1", "theme1_amount", "theme1_pct"])
    df = df.dropna(subset=["theme1"])
    df = df[df["theme1"] != ""]
    if df.empty:
        return dict(days=[], total=0)
    df["date"] = pd.to_datetime(df["date"])

    daily = (df.groupby(["date", "theme1"], as_index=False)
               .agg(theme1_amount=("theme1_amount", "max"),
                    theme1_pct=("theme1_pct", "max"),
                    n_stocks=("code", "nunique")))

    rows = []
    for d, g in daily.groupby("date"):
        g2 = g[g["n_stocks"] >= min_n_stocks]
        if len(g2) < 2:
            continue
        i_amt, i_pct, i_n = g2["theme1_amount"].idxmax(), g2["theme1_pct"].idxmax(), g2["n_stocks"].idxmax()
        if not (i_amt == i_pct == i_n):
            continue
        top = g2.loc[i_amt]
        amt_sorted = g2.sort_values("theme1_amount", ascending=False)
        pct_sorted = g2.sort_values("theme1_pct", ascending=False)
        second_amt = float(amt_sorted.iloc[1]["theme1_amount"])
        second_pct = float(pct_sorted.iloc[1]["theme1_pct"])
        rows.append(dict(
            date=str(d.date()),
            theme1=str(top["theme1"]),
            theme1_pct=round(float(top["theme1_pct"]), 2),
            n_stocks=int(top["n_stocks"]),
            theme1_amount=round(float(top["theme1_amount"]), 0),
            amount_ratio=round(float(top["theme1_amount"]) / second_amt, 2) if second_amt > 0 else None,
            pct_gap=round(float(top["theme1_pct"]) - second_pct, 2),
        ))
    return dict(days=rows, total=len(rows))


def api_period_theme_rank_days(start: date, end: date) -> dict[str, Any]:
    """일별 '주도 테마'(해당 날 theme1_amount 최대인 테마)의 2위 대비 지표.

    api_period_dominant_days와 달리 등락률·종목수 1위 여부나 최소 종목수
    조건 없이, 거래대금 1위 테마를 그대로 주도 테마로 보고 2위 테마와의
    거래대금 비율(amount_ratio)·등락률 차이(pct_gap), 주도 테마 종목수(n_stocks)를 계산한다.
    """
    df = _load(start, end, ["date", "code", "theme1", "theme1_amount", "theme1_pct"])
    df = df.dropna(subset=["theme1"])
    df = df[df["theme1"] != ""]
    if df.empty:
        return dict(days=[], total=0)
    df["date"] = pd.to_datetime(df["date"])

    daily = (df.groupby(["date", "theme1"], as_index=False)
               .agg(theme1_amount=("theme1_amount", "max"),
                    theme1_pct=("theme1_pct", "max"),
                    n_stocks=("code", "nunique")))

    rows = []
    for d, g in daily.groupby("date"):
        if len(g) < 2:
            continue
        amt_sorted = g.sort_values("theme1_amount", ascending=False)
        top = amt_sorted.iloc[0]
        second_amt = float(amt_sorted.iloc[1]["theme1_amount"])
        second_pct = float(amt_sorted.iloc[1]["theme1_pct"])
        rows.append(dict(
            date=str(d.date()),
            theme1=str(top["theme1"]),
            theme1_pct=round(float(top["theme1_pct"]), 2),
            n_stocks=int(top["n_stocks"]),
            theme1_amount=round(float(top["theme1_amount"]), 0),
            amount_ratio=round(float(top["theme1_amount"]) / second_amt, 2) if second_amt > 0 else None,
            pct_gap=round(float(top["theme1_pct"]) - second_pct, 2),
        ))
    return dict(days=rows, total=len(rows))


def api_period_dominant_top_stocks(start: date, end: date, top_rank: int = 2) -> dict[str, Any]:
    """압도적 주도 테마일의 1~top_rank위(theme1_rank) 종목 상세.

    날짜·종목·순위·규모(size_class)·테마·등락률(change_pct)·거래대금(amount_sum, 천억원 단위)
    """
    dom = api_period_dominant_days(start, end)
    days = dom["days"]
    if not days:
        return dict(stocks=[], total=0)

    dom_dates = sorted({d["date"] for d in days})
    df = _load(date.fromisoformat(dom_dates[0]), end,
               ["date", "code", "name", "theme1", "theme1_rank", "size_class",
                "change_pct", "amount_sum"])
    df["date"] = df["date"].astype(str)
    df = df[df["theme1_rank"].notna() & (df["theme1_rank"] <= top_rank)]

    rows = []
    for d in days:
        sub = df[(df["date"] == d["date"]) & (df["theme1"] == d["theme1"])]
        for _, r in sub.iterrows():
            rows.append(dict(
                date=d["date"],
                code=str(r["code"]),
                name=str(r["name"]),
                theme1_rank=int(r["theme1_rank"]),
                size_class=str(r["size_class"]) if pd.notna(r["size_class"]) else None,
                theme1=d["theme1"],
                change_pct=round(float(r["change_pct"]), 2) if pd.notna(r["change_pct"]) else None,
                amount_100b=round(float(r["amount_sum"]) / 1e11, 2) if pd.notna(r["amount_sum"]) else None,
            ))
    rows.sort(key=lambda x: (x["date"], x["theme1_rank"], -(x["amount_100b"] or 0)))
    return dict(stocks=_clean(rows), total=len(rows))


def api_period_leaders(start: date, end: date) -> dict[str, Any]:
    """§2 강세 주도종목."""
    df = _load(start, end,
               ["date", "code", "name", "theme1",
                "contrib_score", "theme1_rank", "change_pct"])
    df = df.dropna(subset=["code", "contrib_score"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    # 누적수익률 = 종목의 (기간 시작일 또는 첫 등장일 종가 대비 종료일 종가) 등락률
    cum, _theme_map, _dates, _name_map, _jump_codes = _stock_cum_frame(start, end)

    dates      = sorted(df["date"].unique())
    total_days = len(dates)

    df["theme_n"]  = df.groupby(["date", "theme1"])["code"].transform("count")
    df["rank_pct"] = np.where(
        df["theme1_rank"].notna() & (df["theme_n"] > 1),
        1.0 - (df["theme1_rank"] - 1) / (df["theme_n"] - 1),
        1.0,
    )

    results = []
    for code, g in df.groupby("code"):
        g = g.sort_values("date")
        n = len(g)
        if n < 3:
            continue
        max_s = float(g["contrib_score"].max())
        if max_s < 7:
            continue
        recent3 = float(g["contrib_score"].tail(3).mean())
        if recent3 < 3:
            continue

        # 순위모멘텀: 조회기간의 절대 중간날짜(mid) 기준 전/후반 분리 방식은 같은 종목의
        # 같은 원자료라도 조회 기간(start/end)에 따라 mid가 달라져 momentum 부호 자체가
        # 뒤집히는 버그가 있었음(2026-06-19 발견 — 미래에셋생명 3개월 조회 momentum=1.0
        # vs 06-08~06-18 조회 momentum=0.0). 외부 기준점 대신 그 종목 자신의 "연속 등장일
        # 사이" rank_pct 증감만 비교하도록 교체 — 조회 구간을 좁혀도 잘리는 건 양끝
        # 비교쌍뿐이라 결과가 일관됨.
        rank_seq = g["rank_pct"].tolist()
        ups   = sum(1 for i in range(1, n) if rank_seq[i] > rank_seq[i - 1])
        downs = sum(1 for i in range(1, n) if rank_seq[i] < rank_seq[i - 1])
        mom = (ups - downs) / (n - 1) if n > 1 else 0.0
        if mom <= 0:
            continue

        avg_s     = float(g["contrib_score"].mean())
        first_sig = g.loc[g["contrib_score"] >= 7, "date"].min()
        composite = (avg_s / 10) * math.log1p(n) * (1 + mom)  # [F6]

        if code in cum.columns:
            cumret_pct = round(float(cum[code].iloc[-1]), 2)
        else:
            cumret_pct = round(_cumret(g["change_pct"]), 2)

        results.append(dict(
            code              = code,
            name              = str(g["name"].iloc[-1]),
            theme1            = str(g["theme1"].dropna().mode().iloc[0]) if g["theme1"].notna().any() else "",
            appear_days       = n,
            coverage_pct      = round(n / max(total_days, 1) * 100, 1),
            max_score         = round(max_s, 1),
            avg_score         = round(avg_s, 2),
            recent3_avg       = round(recent3, 2),
            cumret_pct        = cumret_pct,
            rank_momentum     = round(mom, 4),
            composite         = round(composite, 4),
            first_signal_date = str(first_sig.date()) if pd.notna(first_sig) else None,
        ))

    out = pd.DataFrame(results)
    if out.empty:
        return dict(leaders=[], total=0)
    out = out.nlargest(30, "composite")
    return dict(leaders=_clean(out.to_dict("records")), total=len(out))


def api_period_breadth(start: date, end: date) -> dict[str, Any]:
    """§3 강세 분포 (v1 버킷 기준 유지)."""
    df = _load(start, end, ["date", "code", "market", "size_class", "contrib_score"])
    df = df.dropna(subset=["contrib_score", "size_class", "market"])

    def bkt(s: float) -> str:
        if s >= 4:  return "주도"
        if s >= 1:  return "강세"
        if s >= -1: return "중립"
        return "약세"

    df["bucket"] = df["contrib_score"].apply(bkt)

    def pivot(key: str) -> dict:
        cnt   = df.groupby([key, "bucket"])["code"].count().reset_index(name="n")
        total = df.groupby(key)["code"].count()
        out: dict = {}
        for kv, sub in cnt.groupby(key):
            d    = sub.set_index("bucket")["n"].to_dict()
            tot  = int(total.get(kv, 1))
            lead = int(d.get("주도", 0))
            bull = int(d.get("강세", 0))
            out[str(kv)] = dict(
                주도=lead, 강세=bull,
                중립=int(d.get("중립", 0)), 약세=int(d.get("약세", 0)),
                total=tot, bull_ratio=round((lead + bull) / max(tot, 1), 4),
            )
        return out

    return dict(by_size=pivot("size_class"), by_market=pivot("market"))


def api_period_leader_events(start: date, end: date) -> dict[str, Any]:
    """§4 강세주도 이벤트 (기여점수 ≥7)."""
    df = _load(start, end,
               ["date", "code", "name", "theme1",
                "contrib_score", "theme1_rank"])
    df = df[df["contrib_score"] >= 7].copy()
    if df.empty:
        return dict(events=[], total=0)

    df["date"] = pd.to_datetime(df["date"])
    cum = df.groupby("code")["contrib_score"].sum().rename("cum_contrib")
    df  = df.join(cum, on="code")
    df  = df.sort_values(["cum_contrib", "name", "date"],
                         ascending=[False, True, True])
    df["date"] = df["date"].dt.date.astype(str)

    cols = ["date", "code", "name", "theme1",
            "contrib_score", "cum_contrib", "theme1_rank"]
    return dict(
        events=_clean(df[cols].replace({np.nan: None}).to_dict("records")),
        total =len(df),
    )


if __name__ == '__main__':
    df = pd.read_excel('/mnt/user-data/uploads/SSen분석_샘플.xlsx', sheet_name='거래대금')
    r = run(df, '2026-02-06', '2026-04-29')
    pd.set_option('display.width', 200, 'display.max_columns', 20)
    print('=== meta ===', r['meta'])
    print('\n=== 상승 테마 TOP10 (v2) ===')
    print(r['themes']['up'][['누적상승율', '누적거래대금', '상승일비율', '종합점수', '로테이션']].round(1))
    print('\n=== 하락 테마 (v2) ===')
    print(r['themes']['down'][['누적상승율', '누적거래대금', '하락점수']].round(2))
    print('\n=== 부상 테마 (로테이션+) ===')
    print(r['themes']['rotation'][['전반수익', '후반수익', '로테이션', '종합점수']].head(5))
    print('\n=== 강세 주도종목 TOP10 (v2) ===')
    print(r['leaders'].head(10)[['종목명', '테마(1차)', '복합점수', '평균기여점수', '등장일수',
                                 '커버리지', '복리수익률', '순위모멘텀', '첫주도신호일']])
    print('\n=== 강세 분포 ===')
    print(r['breadth'])

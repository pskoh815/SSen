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
from typing import Any

import duckdb

from .perf_timer import timed_db_query

_ROOT   = Path(__file__).resolve().parents[3]
_FDS    = str(_ROOT / "data" / "parquet" / "fact_daily_stock" / "**" / "*.parquet").replace("\\", "/")
_FKOSPI = str(_ROOT / "data" / "parquet" / "fact_kospi"       / "**" / "*.parquet").replace("\\", "/")

MIN_APPEAR_DAYS = 2  # 기간수익률(시작가→종료가) 산출에 필요한 최소 등장일수


def _ym(d: date) -> int:
    return d.year * 100 + d.month


def _load(start: date, end: date, cols: list[str]) -> pd.DataFrame:
    """DuckDB predicate pushdown — yearmonth 파티션 + 날짜 필터."""
    select = ", ".join(cols)
    sql = f"""
        SELECT {select}
        FROM   read_parquet('{_FDS}', hive_partitioning=true)
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


def _stock_cum_frame(start: date, end: date) -> tuple[pd.DataFrame, pd.Series, list]:
    """종목별 일별 누적수익률(첫 등장일 종가 대비, %) 행렬 + 종목→테마(1차) 매핑.

    - 등장하지 않은 날은 직전 종가를 carry-forward
    - 등장일수 < MIN_APPEAR_DAYS 종목은 시작가/종료가 비교가 불가하므로 제외
    """
    df = _load(start, end, ["date", "code", "theme1", "close_price"])
    df["date"] = pd.to_datetime(df["date"])
    dates = sorted(df["date"].unique())
    if not dates:
        return pd.DataFrame(), pd.Series(dtype=object), dates

    counts = df.groupby("code")["date"].nunique()
    valid_codes = counts[counts >= MIN_APPEAR_DAYS].index
    df = df[df["code"].isin(valid_codes)]

    theme_map = (
        df[df["theme1"].notna() & (df["theme1"] != "")]
        .groupby("code")["theme1"].agg(lambda s: s.mode().iloc[0])
    )

    piv  = df.pivot_table(index="date", columns="code", values="close_price", aggfunc="last").reindex(dates)
    piv  = piv.ffill()
    base = piv.bfill().iloc[0]
    cum  = (piv.div(base) - 1) * 100  # 첫 등장일 이전은 NaN
    return cum, theme_map, dates


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
    cum, theme_map, dates = _stock_cum_frame(start, end)
    if cum.empty or theme_map.empty:
        return dict(rising=[], falling=[], rotating=[], all=[])

    theme_cum = _theme_cum_series(cum, theme_map)
    if theme_cum.empty:
        return dict(rising=[], falling=[], rotating=[], all=[])

    final = theme_cum.iloc[-1]
    mid_i = len(dates) // 2 - 1 if len(dates) >= 2 else 0
    mid   = theme_cum.iloc[mid_i]

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
        up = float((cum[codes].iloc[-1].dropna() > 0).mean()) if codes else 0.0
        rows.append(dict(
            theme1=theme,
            appear_days=int(appear_days.get(theme, 0)),
            cumret_pct=round(cr, 2), cumret_first=round(cf, 2), cumret_second=round(cs, 2),
            total_amount=round(float(total_amount.get(theme, 0.0)), 0),
            up_ratio=round(up, 3), rotation=round(cs - cf, 2),
        ))

    r = pd.DataFrame(rows)
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
            "composite", "rotation", "fall_score"]

    def top(df_: pd.DataFrame, col: str, n: int = 10, smallest: bool = False) -> list[dict]:
        df_ = df_.nsmallest(n, col) if smallest else df_.nlargest(n, col)
        return _clean(df_[keep].replace({np.nan: None}).to_dict("records"))

    return dict(
        rising   = top(r, "cumret_pct"),
        falling  = top(r[fm], "cumret_pct", 10, smallest=True) if fm.any() else [],
        rotating = top(r, "rotation"),
        all      = _clean(r[keep].replace({np.nan: None}).to_dict("records")),
    )


def api_period_theme_trend(start: date, end: date, top: int = 20) -> dict[str, Any]:
    """§1-T 테마 추이 — 상위 top개 테마의 기간별 누적수익률 시계열 + 코스피 베이스라인.

    누적수익률(cum) = 테마에 속한 각 종목의, 기간 시작일(또는 첫 등장일) 종가 대비
    해당일 종가의 등락률을 종목수로 단순평균한 값.
    kospi = 코스피지수 종가의 시작일 대비 누적수익률(%) — 같은 기간 내 비교 기준선.
    정렬 = 누적상승율(cumret_pct) 내림차순 (api_period_themes와 동일 기준).
    """
    cum, theme_map, dates = _stock_cum_frame(start, end)
    if cum.empty or theme_map.empty:
        return dict(dates=[], series=[], kospi=[])

    theme_cum = _theme_cum_series(cum, theme_map)
    if theme_cum.empty:
        return dict(dates=[], series=[], kospi=[])

    final = theme_cum.iloc[-1]
    top_themes = list(final.nlargest(top).index)

    kospi_cum = _kospi_cum_series(start, end, dates)

    series = [
        dict(theme1=t, cum=[round(float(v), 2) for v in theme_cum[t]])
        for t in top_themes
    ]
    return dict(
        dates =[pd.Timestamp(d).strftime("%Y-%m-%d") for d in dates],
        series=series,
        kospi =[round(float(v), 2) for v in kospi_cum],
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
    cum, _theme_map, _dates = _stock_cum_frame(start, end)

    dates      = sorted(df["date"].unique())
    total_days = len(dates)
    mid        = dates[len(dates) // 2 - 1] if len(dates) >= 2 else dates[0]

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

        rp1 = g.loc[g["date"] <= mid, "rank_pct"].mean()
        rp2 = g.loc[g["date"] >  mid, "rank_pct"].mean()
        mom = (0.0 if np.isnan(rp2) else float(rp2)) - \
              (0.0 if np.isnan(rp1) else float(rp1))
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

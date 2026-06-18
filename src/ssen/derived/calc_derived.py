"""
파생 컬럼 계산 (거래대금 시트 N~AC열)
스펙: docs/derived_columns.md

입력 (A~M열, API 수집 원본):
    날짜, 순위, 종목코드, 종목명, 시장구분, 시작일기준가, 종료일종가, 대비, 등락률,
    거래량_합계, 거래량_일평균, 거래대금_합계, 거래대금_일평균

부가 입력:
    themes: 테마 시트 (종목코드, 상장주식수, 테마(1차), 테마(2차))
    kospi:  코스피시세 시트 (Date: YYYY-MM-DD str, Increase rate)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _classify_size(market: str, mktcap: float) -> str:
    if pd.isna(mktcap):
        return ""
    if market == "KOSPI":
        if mktcap >= 6.5e12:
            return "대형"
        elif mktcap >= 7e11:
            return "중형"
        else:
            return "소형"
    else:  # KOSDAQ
        if mktcap >= 1e12:
            return "대형"
        elif mktcap >= 3e11:
            return "중형"
        else:
            return "소형"


def _classify_strength(score: float) -> str:
    if pd.isna(score):
        return ""
    if score >= 7:
        return "🔥강세주도"
    elif score >= 4:
        return "↑강세"
    elif score >= 1:
        return "↗약한강세"
    elif score >= -1:
        return "→중립"
    elif score >= -2:
        return "↘약한약세"
    elif score >= -7:
        return "↓약세"
    else:
        return "❄️약세주도"


def _clean_theme(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).replace({"0": "", "nan": "", "None": ""})


def add_derived_columns(df: pd.DataFrame, themes: pd.DataFrame, kospi: pd.DataFrame) -> pd.DataFrame:
    """A~M열 raw df + themes/kospi 참조 → N~AC열 포함 29열 df 반환."""
    df = df.copy()

    # N: 상장주식수
    shares_map = themes.set_index("종목코드")["상장주식수"]
    df["상장주식수"] = df["종목코드"].map(shares_map)

    # O: 코스피 대비 등락률
    date_str = pd.to_datetime(df["날짜"]).dt.strftime("%Y-%m-%d")
    kospi_rate = kospi.set_index("Date")["Increase rate"]
    df["코스피 대비 등락률"] = df["등락률"] - date_str.map(kospi_rate).fillna(0)

    # P: 시가총액
    df["시가총액"] = df["상장주식수"] * df["종료일종가"]

    # Q: 시총 대비 거래대금 증가율 (상한 500)
    df["시총 대비 거래대금 증가율"] = (df["거래대금_일평균"] / df["시가총액"] * 100).clip(upper=500)

    # R: 규모
    df["규모"] = [
        _classify_size(m, c) for m, c in zip(df["시장구분"], df["시가총액"])
    ]

    # S: 기여점수
    score = (
        np.clip(df["등락률"] / 30, -1, 1) * 0.5
        + np.clip(df["코스피 대비 등락률"] / 38, -1, 1) * 0.3
        + np.clip(df["시총 대비 거래대금 증가율"] / 500, 0, 1) * np.sign(df["등락률"]) * 0.2
    ) * 10
    df["기여점수"] = np.clip(score, -10, 10).round(1)

    # T: 기여도순위 (날짜별 기여점수 내림차순, 100위 초과는 None)
    df["기여도순위"] = df.groupby("날짜")["기여점수"].rank(ascending=False, method="min")
    df.loc[df["기여도순위"] > 100, "기여도순위"] = np.nan

    # U/Z: 테마(1차/2차)
    theme1_map = themes.set_index("종목코드")["테마(1차)"]
    theme2_map = themes.set_index("종목코드")["테마(2차)"]
    df["테마(1차)"] = _clean_theme(df["종목코드"].map(theme1_map))
    df["테마(2차)"] = _clean_theme(df["종목코드"].map(theme2_map))

    # V/Y: 테마 순위
    df["테마(1차) 순위"] = df.groupby(["날짜", "테마(1차)"])["기여점수"].rank(ascending=False, method="min")
    df["테마(2차) 순위"] = df.groupby(["날짜", "테마(2차)"])["기여점수"].rank(ascending=False, method="min")

    # W/AA: 테마 거래대금
    df["테마(1차) 거래대금"] = df.groupby(["날짜", "테마(1차)"])["거래대금_일평균"].transform("sum")
    df["테마(2차) 거래대금"] = df.groupby(["날짜", "테마(2차)"])["거래대금_일평균"].transform("sum")

    # X/AB: 테마 등락률
    df["테마(1차) 등락률"] = df.groupby(["날짜", "테마(1차)"])["코스피 대비 등락률"].transform("mean")
    df["테마(2차) 등락률"] = df.groupby(["날짜", "테마(2차)"])["코스피 대비 등락률"].transform("mean")

    # AC: 강약 판정
    df["강약 판정"] = df["기여점수"].apply(_classify_strength)

    return df

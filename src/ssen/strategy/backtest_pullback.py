"""
눌림목 백테스트 v3 (OHLCV 기반)

━━━━ 상승추세 조건 (3가지 동시 충족) ━━━━
  1) 완전 정배열 : close > MA5 > MA10 > MA20 > MA60 > MA120
  2) MA5 52주 신고가 : MA5 > max(MA5[-1] ~ MA5[-240])
  3) MA10·MA20·MA60 밀집 (5% 이내) :
       MIN(MA10,MA20,MA60) × 1.05 > MAX(MA10,MA20,MA60)
       ↔ 정배열 하에서  MA10 / MA60 < 1.05

━━━━ 눌림목 진입 ━━━━
  상승추세를 최근 7봉 내에 확인 후,
  직전 7봉 중 봉 몸통(|종가-시가|)이 가장 작은 날이면서
  그 날 종가가 MA7 ±3% 이내 → 당일 종가 매수

━━━━ 청산 ━━━━
  종가 < MA7  (7일선 이탈)  |  안전 손절 -15%  |  최대 120거래일

Usage:
    python -m ssen.strategy.backtest_pullback
    python -m ssen.strategy.backtest_pullback --no-filter
    python -m ssen.strategy.backtest_pullback --stop 15 --max-hold 120
"""
from __future__ import annotations

import argparse
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT         = Path(__file__).resolve().parents[3]
OHLCV_DIR    = ROOT / "data" / "market" / "ohlcv"
BUFFETT_XLSX = ROOT / "data" / "buffett_kr_200_v2.xlsx"
SSEN_DIR     = ROOT / "data" / "parquet" / "fact_daily_stock"
ADR_DIR      = ROOT / "data" / "parquet" / "fact_adr"

START_DATE       = date(2020, 1, 1)
END_DATE         = date(2026, 5, 31)
RATIO_THRESHOLD  = 5.0      # --no-filter 시 비활성
MA7_NEAR_HIGH    = 1.03     # 종가 ≤ MA7 × 1.03
MA7_NEAR_LOW     = 0.97     # 종가 ≥ MA7 × 0.97
MA_CLUSTER_RATIO = 1.05     # MA10 / MA60 < 1.05
UPTREND_LOOKBACK = 7        # 상승추세 유효 기간 (최근 N봉)
STOP_LOSS_PCT    = 15.0     # 안전 손절 (MA7 이탈이 주 청산)
MAX_HOLD_DAYS    = 120      # 최대 보유일
FEE_PCT          = 0.3


# ── 데이터 로드 ──────────────────────────────────────────────────────────────

def load_ohlcv() -> pd.DataFrame:
    print("[1] OHLCV Parquet 로드...")
    parts = sorted(OHLCV_DIR.glob("yearmonth=*/*/data.parquet"))
    dfs = []
    for p in parts:
        ym   = p.parent.parent.name.split("=")[1]
        ym_d = date(int(ym[:4]), int(ym[4:6]), 1)
        if ym_d > END_DATE:
            continue
        dfs.append(pd.read_parquet(
            p, columns=["date","code","name","market",
                        "open","high","low","close","volume"]))

    df = pd.concat(dfs, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[(df["date"] >= START_DATE) & (df["date"] <= END_DATE)]
    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open","close","high","low"])
    df = df[df["close"] > 0]
    df = df.sort_values(["code","date"]).reset_index(drop=True)
    print(f"  {len(df):,}행  |  {df['code'].nunique():,}종목  |  {df['date'].nunique()}거래일")
    print(f"  기간: {df['date'].min()} ~ {df['date'].max()}")
    return df


# ── 유니버스 필터 ─────────────────────────────────────────────────────────────

def _exclude_noise(df: pd.DataFrame) -> pd.DataFrame:
    """
    정밀 노이즈 제거 — 전 행 기준으로 검색해 이름 변경·인코딩 차이 대응.

    ① 스팩(SPAC)  : 종목명에 '스팩' 포함 (코드 마지막 자리 0이라 코드 불가)
    ② 우선주(코드): 종목코드 마지막 자리 5·7·9
                   (KRX 규칙: 1우=5, 2우=7, 3우=9)
    ③ 우선주(이름): 종목명이 '우' 또는 '우A'~'우Z' 로 끝남
    ④ 리츠(REITs) : 종목명에 '리츠' 포함 (가격·배당 구조 상이)
    """
    # 전 기간 any 검색 — groupby.last()보다 누락 방지
    spac  = set(df[df["name"].str.contains("스팩", na=False)]["code"])
    pref_c = set(df[df["code"].str[-1].isin(["5","7","9"])]["code"])
    pref_n = set(df[df["name"].str.contains(r"우$|우[A-Za-z]$",
                                             na=False, regex=True)]["code"])
    reit  = set(df[df["name"].str.contains("리츠", na=False)]["code"])

    bad = spac | pref_c | pref_n | reit
    filtered = df[~df["code"].isin(bad)].copy()

    n_before = df["code"].nunique()
    n_after  = filtered["code"].nunique()
    print(f"  노이즈 제거: {n_before:,} → {n_after:,}종목  "
          f"(스팩 {len(spac)} | 우선주(코드) {len(pref_c)} | "
          f"우선주(이름) {len(pref_n)} | 리츠 {len(reit)})")
    return filtered


def load_buffett_codes() -> set[str]:
    """buffett_kr_200_v2.xlsx → 6자리 종목코드 set."""
    raw = pd.read_excel(BUFFETT_XLSX, sheet_name="버핏형 국내상장기업")
    result: set[str] = set()
    for v in raw["종목코드"].dropna():
        try:
            result.add(str(int(float(v))).zfill(6))
        except (ValueError, TypeError):
            pass
    return result


def load_ssen_leader_filter(mode: str, window: int = 20,
                            rank_top: int = 50) -> dict:
    """
    SSen fact_daily_stock Parquet에서 날짜별 주도주 필터를 빌드.
    결과: {date -> frozenset(codes)}  — 해당 날 이전 window일 내 자격 취득 종목

    mode='theme'  : 최근 window일 내 주도 테마(theme1_amount 1위)에 포함된 종목
    mode='leader' : 최근 window일 내 주도주(주도 테마 내 rank 최솟값)였던 종목
    mode='rank'   : 최근 window일 내 전체 거래대금 순위 rank ≤ rank_top 이었던 종목
    """
    print("[2] SSen 주도주 필터 로드...")
    col_need = ["date","code","rank","theme1","theme1_amount"]
    parts = sorted(SSEN_DIR.glob("*/data.parquet"))
    dfs = []
    for p in parts:
        ym = int(p.parent.name.split("=")[1])
        if ym <= int(END_DATE.strftime("%Y%m")):
            dfs.append(pd.read_parquet(p, columns=col_need))
    raw = pd.concat(dfs, ignore_index=True)
    raw["date"] = pd.to_datetime(raw["date"]).dt.date

    # 날짜별 자격 종목 집합
    daily_qualify: dict[object, set] = {}
    for dt, g in raw.groupby("date"):
        if mode == "rank":
            daily_qualify[dt] = set(g[g["rank"] <= rank_top]["code"].tolist())
        else:
            g2 = g[g["theme1"].notna()]
            if g2.empty:
                daily_qualify[dt] = set()
                continue
            theme_amt = g2.groupby("theme1")["theme1_amount"].first()
            top_theme = theme_amt.idxmax()
            in_theme  = g2[g2["theme1"] == top_theme]
            if mode == "theme":
                daily_qualify[dt] = set(in_theme["code"].tolist())
            else:  # leader
                leader = in_theme.loc[in_theme["rank"].idxmin(), "code"]
                daily_qualify[dt] = {leader}

    # rolling window 누적
    sorted_dates = sorted(daily_qualify.keys())
    result: dict = {}
    for i, dt in enumerate(sorted_dates):
        pool: set = set()
        for j in range(max(0, i - window + 1), i + 1):
            pool |= daily_qualify[sorted_dates[j]]
        result[dt] = frozenset(pool)

    n_days  = len(result)
    avg_cnt = sum(len(v) for v in result.values()) / max(1, n_days)
    labels  = {"theme": "주도 테마", "leader": "주도주", "rank": f"거래대금 상위{rank_top}위"}
    print(f"  SSen {labels.get(mode, mode)} 필터 (최근 {window}거래일): "
          f"{n_days}거래일  |  일평균 유효 {avg_cnt:.0f}종목")
    return result


# ── 거시 환경 필터 ────────────────────────────────────────────────────────────

def build_samsung_kospi_filter(window: int = 20) -> dict:
    """
    삼성전자(005930) 상대강도 필터.
    최근 window거래일 누적 로그수익률: 삼성전자 ≥ KOSPI 시총가중 → True
    lookahead 방지: t일 진입 결정은 t-1일까지의 window일 수익률 사용 (shift(1))
    """
    print(f"[M1] 삼성전자 vs KOSPI 상대강도 필터 (window={window})...")
    parts = sorted(OHLCV_DIR.glob("yearmonth=*/KOSPI/data.parquet"))
    dfs: list[pd.DataFrame] = []
    for p in parts:
        ym   = p.parent.parent.name.split("=")[1]
        ym_d = date(int(ym[:4]), int(ym[4:6]), 1)
        if ym_d > END_DATE:
            continue
        dfs.append(pd.read_parquet(
            p, columns=["date", "code", "change_pct", "mktcap"]))
    raw = pd.concat(dfs, ignore_index=True)
    raw["date"]       = pd.to_datetime(raw["date"]).dt.date
    raw["change_pct"] = pd.to_numeric(raw["change_pct"], errors="coerce")
    raw["mktcap"]     = pd.to_numeric(raw["mktcap"],     errors="coerce")
    raw = raw.dropna(subset=["change_pct", "mktcap"])
    raw = raw[(raw["date"] >= START_DATE) & (raw["date"] <= END_DATE)
              & (raw["mktcap"] > 0)]

    # 삼성전자 일별 등락률
    sam = (raw[raw["code"] == "005930"]
           .sort_values("date")
           .set_index("date")["change_pct"]
           .rename("sam_chg"))

    # KOSPI 시총가중 일별 등락률 (mktcap 가중 평균)
    raw["wchg"] = raw["change_pct"] * raw["mktcap"]
    daily = raw.groupby("date").agg(wsum=("wchg", "sum"), tmkt=("mktcap", "sum"))
    daily["kospi_chg"] = daily["wsum"] / daily["tmkt"]

    merged = daily[["kospi_chg"]].join(sam, how="inner").sort_index()

    # 로그 누적 수익률 비교 (lookahead 방지: shift(1) 후 rolling)
    merged["sam_log"]  = np.log1p(merged["sam_chg"]   / 100)
    merged["ksp_log"]  = np.log1p(merged["kospi_chg"] / 100)
    merged["sam_cum"]  = merged["sam_log"].shift(1).rolling(window, min_periods=window).sum()
    merged["ksp_cum"]  = merged["ksp_log"].shift(1).rolling(window, min_periods=window).sum()
    merged["market_on"] = merged["sam_cum"] >= merged["ksp_cum"]

    n_on    = int(merged["market_on"].sum())
    n_total = int(merged["market_on"].notna().sum())
    print(f"  삼성 우위 거래일: {n_on}/{n_total} ({n_on / n_total * 100:.1f}%)")
    return {dt: bool(v) for dt, v in merged["market_on"].dropna().items()}


def build_adr_filter(window: int = 10, thresh: float = 0.45) -> dict:
    """
    ADR(시장 폭) 필터.
    rolling window일 평균 ADR ≥ thresh → True (광범위한 상승 참여)
    ADR = 상승종목수 / (상승+하락종목수), 보합 제외
    lookahead 방지: 당일 ADR은 장 마감 후 계산 → shift(1)로 전일까지 반영
    """
    print(f"[M2] ADR 시장 폭 필터 (window={window}, thresh={thresh})...")
    parts = sorted(OHLCV_DIR.glob("yearmonth=*/*/data.parquet"))
    dfs: list[pd.DataFrame] = []
    for p in parts:
        ym   = p.parent.parent.name.split("=")[1]
        ym_d = date(int(ym[:4]), int(ym[4:6]), 1)
        if ym_d > END_DATE:
            continue
        dfs.append(pd.read_parquet(p, columns=["date", "code", "change_pct"]))
    raw = pd.concat(dfs, ignore_index=True)
    raw["date"]       = pd.to_datetime(raw["date"]).dt.date
    raw["change_pct"] = pd.to_numeric(raw["change_pct"], errors="coerce")
    raw = raw.dropna(subset=["change_pct"])
    raw = raw[(raw["date"] >= START_DATE) & (raw["date"] <= END_DATE)]

    raw["up"]   = (raw["change_pct"] > 0).astype(int)
    raw["down"] = (raw["change_pct"] < 0).astype(int)
    daily = raw.groupby("date").agg(up=("up", "sum"), down=("down", "sum"))
    daily["adr"]    = daily["up"] / (daily["up"] + daily["down"])
    daily["adr_ma"] = daily["adr"].shift(1).rolling(window, min_periods=window).mean()
    daily["market_on"] = daily["adr_ma"] >= thresh

    n_on    = int(daily["market_on"].sum())
    n_total = int(daily["market_on"].notna().sum())
    print(f"  ADR 충족 거래일: {n_on}/{n_total} ({n_on / n_total * 100:.1f}%)")
    return {dt: bool(v) for dt, v in daily["market_on"].dropna().items()}


def build_bullbear_filter(mode: str = "watch") -> dict:
    """
    BullBear 3중 확인 매크로 필터 (BR₅ + McClellan + A/D Line).

    지표 계산:
      BR    = (KP상승+KD상승) / (KP상승+KP하락+KD상승+KD하락)
      BR₅   = BR.rolling(5).mean()
      순상승 = (KP상승+KD상승) - (KP하락+KD하락)
      McClellan = EMA10(순상승) - EMA39(순상승)
      A/D Line 상승 = 순상승 > 0 (당일 누적 증가)

    매수점수 N:
      (BR₅ > 0.55)*1 + (McClellan > 0)*1 + (순상승 > 0)*1

    mode='watch'   : N >= 2 → True  (매수주시 이상)
    mode='confirm' : 3일 연속 N >= 2 → True  (매수확정)

    lookahead 방지: shift(1) 적용 — 진입일(t)의 신호는 t-1까지 데이터 기준
    """
    print(f"[M3] BullBear 3중확인 필터 (mode={mode})...")
    parts = sorted(ADR_DIR.glob("*/data.parquet"))
    dfs: list[pd.DataFrame] = []
    for p in parts:
        ym   = p.parent.name.split("=")[1]
        ym_d = date(int(ym[:4]), int(ym[4:6]), 1)
        if ym_d > END_DATE:
            continue
        dfs.append(pd.read_parquet(p, columns=["date", "index_name", "up_count", "down_count"]))
    raw = pd.concat(dfs, ignore_index=True)
    raw["date"]       = pd.to_datetime(raw["date"]).dt.date
    raw["up_count"]   = pd.to_numeric(raw["up_count"],   errors="coerce").fillna(0)
    raw["down_count"] = pd.to_numeric(raw["down_count"], errors="coerce").fillna(0)
    raw = raw[(raw["date"] >= START_DATE) & (raw["date"] <= END_DATE)]

    # 코스피 + 코스닥 합산
    daily = (raw.groupby("date")
             .agg(up=("up_count", "sum"), dn=("down_count", "sum"))
             .sort_index())

    # BR = up / (up + dn)
    total = daily["up"] + daily["dn"]
    daily["br"]  = daily["up"] / total.replace(0, np.nan)
    daily["br5"] = daily["br"].rolling(5, min_periods=5).mean()

    # 순상승 (Net Advance)
    daily["net_adv"] = daily["up"] - daily["dn"]

    # McClellan = EMA10 - EMA39 (adjust=False = 재귀 EMA, Excel과 동일)
    ema10 = daily["net_adv"].ewm(span=10, adjust=False).mean()
    ema39 = daily["net_adv"].ewm(span=39, adjust=False).mean()
    daily["mcclellan"] = ema10 - ema39

    # 매수 점수 (lookahead 방지: shift(1))
    br5_ok  = (daily["br5"].shift(1)       > 0.55).astype(int)
    mcc_ok  = (daily["mcclellan"].shift(1)  > 0.0 ).astype(int)
    adv_ok  = (daily["net_adv"].shift(1)    > 0.0 ).astype(int)
    daily["buy_score"] = br5_ok + mcc_ok + adv_ok

    if mode == "br5_only":
        daily["market_on"] = br5_ok == 1
    elif mode == "confirm":
        # 3일 연속 buy_score >= 2
        sig2 = (daily["buy_score"] >= 2).astype(int)
        daily["market_on"] = (sig2.rolling(3, min_periods=3).min() == 1)
    else:  # watch
        daily["market_on"] = daily["buy_score"] >= 2

    n_on    = int(daily["market_on"].sum())
    n_total = int(daily["market_on"].notna().sum())
    print(f"  BullBear 충족 거래일: {n_on}/{n_total} ({n_on / n_total * 100:.1f}%)")
    return {dt: bool(v) for dt, v in daily["market_on"].dropna().items()}


def build_adr_filter_multi(windows: list[int],
                           thresholds: list[float]) -> dict[tuple, dict]:
    """
    OHLCV 1회 로드로 (window, thresh) 조합 전체 ADR 필터 행렬 생성.
    반환: {(window, thresh): {date -> bool}}
    """
    print(f"[SWEEP] ADR 필터 행렬 빌드 "
          f"({len(windows)} windows × {len(thresholds)} thresholds = "
          f"{len(windows)*len(thresholds)}조합)...")
    parts = sorted(OHLCV_DIR.glob("yearmonth=*/*/data.parquet"))
    dfs: list[pd.DataFrame] = []
    for p in parts:
        ym   = p.parent.parent.name.split("=")[1]
        ym_d = date(int(ym[:4]), int(ym[4:6]), 1)
        if ym_d > END_DATE:
            continue
        dfs.append(pd.read_parquet(p, columns=["date", "code", "change_pct"]))
    raw = pd.concat(dfs, ignore_index=True)
    raw["date"]       = pd.to_datetime(raw["date"]).dt.date
    raw["change_pct"] = pd.to_numeric(raw["change_pct"], errors="coerce")
    raw = raw.dropna(subset=["change_pct"])
    raw = raw[(raw["date"] >= START_DATE) & (raw["date"] <= END_DATE)]

    raw["up"]   = (raw["change_pct"] > 0).astype(int)
    raw["down"] = (raw["change_pct"] < 0).astype(int)
    daily = raw.groupby("date").agg(up=("up", "sum"), down=("down", "sum"))
    daily["adr"] = daily["up"] / (daily["up"] + daily["down"])

    results: dict[tuple, dict] = {}
    for window in windows:
        adr_ma = daily["adr"].shift(1).rolling(window, min_periods=window).mean()
        for thresh in thresholds:
            market_on = adr_ma >= thresh
            results[(window, thresh)] = {
                dt: bool(v) for dt, v in market_on.dropna().items()
            }
    print(f"  빌드 완료: {len(results)}개 조합")
    return results


def filter_universe(df: pd.DataFrame, ratio: float,
                    buffett: bool = False) -> tuple[pd.DataFrame, list[str]]:
    # 스팩·우선주 항상 제외
    df = _exclude_noise(df)

    # 버핏 리스트 필터 (단독 적용, ratio 필터와 함께 쓰지 않음)
    if buffett:
        b_codes = load_buffett_codes()
        matched = set(df["code"].unique()) & b_codes
        missing = b_codes - matched
        print(f"  버핏 리스트: 94종목 → OHLCV 매칭 {len(matched)}종목  "
              f"(미수록 {len(missing)}종목)")
        if missing:
            # 미수록 종목 이름 출력 (확인용)
            raw = pd.read_excel(BUFFETT_XLSX, sheet_name="버핏형 국내상장기업")
            miss_map = {}
            for _, r in raw.iterrows():
                try:
                    c = str(int(float(r["종목코드"]))).zfill(6)
                    if c in missing:
                        miss_map[c] = r["종목명"]
                except (ValueError, TypeError):
                    pass
            for c, n in sorted(miss_map.items()):
                print(f"    미수록: {c} {n}")
        codes = sorted(matched)
        return df[df["code"].isin(codes)].copy(), codes

    if ratio <= 0:
        print(f"  유니버스: 전 종목 ({df['code'].nunique():,}종목)")
        return df, df["code"].unique().tolist()

    print(f"  최고가/최저가 ≥ {ratio}배 필터...")
    stats = (df.groupby("code")
             .agg(min_low=("low","min"), max_high=("high","max"),
                  name=("name","last"))
             .reset_index())
    stats = stats[stats["min_low"] > 0].copy()
    stats["ratio"] = stats["max_high"] / stats["min_low"]
    q = stats[stats["ratio"] >= ratio]
    codes = q["code"].tolist()
    print(f"  {ratio}배 이상: {len(codes)}종목")
    return df[df["code"].isin(codes)].copy(), codes


# ── 이동평균 + 신호 컬럼 ────────────────────────────────────────────────────

def _add_signals(g: pd.DataFrame) -> pd.DataFrame:
    g = g.copy()
    c, o = g["close"], g["open"]

    # ── 이동평균 ──────────────────────────────────────────────────────────
    g["MA5"]   = c.rolling(5,   min_periods=5).mean()
    g["MA7"]   = c.rolling(7,   min_periods=7).mean()
    g["MA10"]  = c.rolling(10,  min_periods=10).mean()
    g["MA20"]  = c.rolling(20,  min_periods=20).mean()
    g["MA60"]  = c.rolling(60,  min_periods=60).mean()
    g["MA120"] = c.rolling(120, min_periods=120).mean()

    # ── 상승추세 조건 1: 완전 정배열 ─────────────────────────────────────
    cond1 = (
        (c          > g["MA5"])  &
        (g["MA5"]   > g["MA10"]) &
        (g["MA10"]  > g["MA20"]) &
        (g["MA20"]  > g["MA60"]) &
        (g["MA60"]  > g["MA120"])
    )

    # ── 상승추세 조건 2: MA5 > 과거 240거래일(52주) MA5 최고값 ───────────
    ma5_hist_max = g["MA5"].shift(1).rolling(240, min_periods=240).max()
    cond2 = g["MA5"] > ma5_hist_max

    # ── 상승추세 조건 3: MA10·MA20·MA60 5% 이내 밀집 ─────────────────────
    # 정배열 하에서 MA10(최대) / MA60(최소) < 1.05
    cond3 = g["MA10"] < g["MA60"] * MA_CLUSTER_RATIO

    # ── 통합 상승추세 ─────────────────────────────────────────────────────
    g["full_uptrend"] = cond1 & cond2 & cond3

    # 최근 UPTREND_LOOKBACK봉 내 한 번이라도 full_uptrend = True
    fut_arr = g["full_uptrend"].to_numpy(dtype=float)
    in_up = (
        pd.Series(fut_arr)
        .rolling(UPTREND_LOOKBACK, min_periods=1)
        .max()
        .to_numpy()
    )
    g["in_uptrend"] = (in_up == 1.0)

    # ── 눌림목 조건 ───────────────────────────────────────────────────────
    body = (c - o).abs()
    g["body"]      = body

    # ① 최근 7봉 중 몸통(|종가-시가|)이 가장 작은 날 (1원 오차 허용)
    g["body_7min"]    = body.rolling(7, min_periods=7).min()
    g["is_min_body7"] = body <= g["body_7min"] + 1

    # ② 최근 7봉 중 MA7에 종가가 가장 근접한 날 (1원 오차 허용)
    dist_to_ma7 = (c - g["MA7"]).abs()
    g["dist_ma7"]       = dist_to_ma7
    g["dist_ma7_7min"]  = dist_to_ma7.rolling(7, min_periods=7).min()
    g["is_closest_ma7"] = dist_to_ma7 <= g["dist_ma7_7min"] + 1

    # ── 볼린저 밴드 (240, 2) ──────────────────────────────────────────────
    sma240 = c.rolling(240, min_periods=240).mean()
    std240 = c.rolling(240, min_periods=240).std()
    g["BB_mid240"]   = sma240
    g["BB_upper240"] = sma240 + 2 * std240

    return g


# ── 단일 종목 시뮬레이션 ─────────────────────────────────────────────────────

def _simulate(sdf: pd.DataFrame,
              stop_loss: float,
              max_hold: int,
              ssen_filter: dict | None = None,
              macro_filter: dict | None = None,
              fixed_stop_pct: float | None = None,
              bb_volatility: bool = False) -> list[dict]:
    """
    fixed_stop_pct: MA7 이탈 대신 매수가 대비 고정 % 손절 사용.
    bb_volatility : BB상한(240,2) > BB중심(240,2)*1.5 종목만 진입 허용.
    """
    # 유효 행: MA120이 존재
    sdf = sdf[sdf["MA120"].notna()].reset_index(drop=True)
    if len(sdf) < 10:
        return []

    code   = sdf.at[0, "code"]
    name   = sdf.at[0, "name"]
    market = sdf.at[0, "market"]

    dates          = sdf["date"].to_numpy()
    closes         = sdf["close"].to_numpy(dtype=float)
    ma7s           = sdf["MA7"].to_numpy(dtype=float)
    in_uptrend     = sdf["in_uptrend"].to_numpy(dtype=bool)
    is_min_body7   = sdf["is_min_body7"].to_numpy(dtype=bool)
    is_closest_ma7 = sdf["is_closest_ma7"].to_numpy(dtype=bool)
    full_uptrend   = sdf["full_uptrend"].to_numpy(dtype=bool)

    if bb_volatility:
        bb_upper = sdf["BB_upper240"].to_numpy(dtype=float)
        bb_mid   = sdf["BB_mid240"].to_numpy(dtype=float)
    else:
        bb_upper = bb_mid = None

    trades = []
    in_pos = False
    ep = ed = ei = None
    peak_pct = peak_date = peak_px = None

    for i in range(len(sdf)):
        c   = closes[i]
        ma7 = ma7s[i]

        if not in_pos:
            # SSen 주도주 조건 (ssen_filter가 있을 때만 체크)
            # ssen_filter: {date -> frozenset(codes)}  최근 N일 내 주도주 이력 보유 종목
            if ssen_filter is not None:
                dt = dates[i]
                ssen_ok = dt in ssen_filter and code in ssen_filter[dt]
            else:
                ssen_ok = True

            # 거시 환경 필터 (삼성전자 강도 또는 ADR)
            if macro_filter is not None:
                macro_ok = macro_filter.get(dates[i], False)
            else:
                macro_ok = True

            # 볼린저 밴드(240,2) 변동성 필터: BB상한 > BB중심*1.5
            if bb_volatility and bb_upper is not None:
                bu, bm = bb_upper[i], bb_mid[i]
                bb_ok = (not np.isnan(bu)) and (not np.isnan(bm)) and (bu > bm * 1.5)
            else:
                bb_ok = True

            # 진입: 상승추세(최근 7봉 내) + 7봉 최소 몸통 + MA7 근처 + SSen + 거시 + BB조건
            if (macro_ok
                    and ssen_ok
                    and bb_ok
                    and in_uptrend[i]
                    and is_min_body7[i]
                    and is_closest_ma7[i]
                    and not np.isnan(ma7)):
                in_pos   = True
                ep       = c
                ed       = dates[i]
                ei       = i
                peak_pct  = 0.0
                peak_date = dates[i]
                peak_px   = int(c)
        else:
            pnl  = (c - ep) / ep * 100
            hold = i - ei

            # MFE(최고 수익률) 갱신
            if pnl > peak_pct:
                peak_pct  = pnl
                peak_date = dates[i]
                peak_px   = int(c)

            reason = None
            if fixed_stop_pct is not None:
                # 고정 % 손절 모드: MA7 이탈 없이 매수가 대비 고정 손절
                if pnl <= -fixed_stop_pct:
                    reason = "stop_loss"
                elif hold >= max_hold:
                    reason = "time_stop"
            else:
                # 기존 모드: MA7 이탈 주 청산 + 안전 손절
                if not np.isnan(ma7) and c < ma7:
                    reason = "ma7_break"
                elif pnl <= -stop_loss:
                    reason = "stop_loss"
                elif hold >= max_hold:
                    reason = "time_stop"

            if reason:
                net = pnl - FEE_PCT
                trades.append({
                    "code": code, "name": name, "market": market,
                    "entry_date":  ed,       "entry_price": int(ep),
                    "exit_date":   dates[i], "exit_price":  int(c),
                    "exit_reason": reason,
                    "pnl_pct":     round(pnl,      4),
                    "net_pnl_pct": round(net,      4),
                    "hold_days":   hold,
                    # MFE 필드
                    "peak_pct":    round(peak_pct,  4),
                    "peak_date":   peak_date,
                    "peak_price":  peak_px,
                    "giveback_pct": round(peak_pct - pnl, 4),
                })
                in_pos = False

    if in_pos:
        trades.append({
            "code": code, "name": name, "market": market,
            "entry_date": ed, "entry_price": int(ep),
            "exit_date": None, "exit_price": None,
            "exit_reason": "open",
            "pnl_pct": None, "net_pnl_pct": None,
            "hold_days": len(sdf) - 1 - ei,
            "peak_pct": round(peak_pct, 4), "peak_date": peak_date,
            "peak_price": peak_px,
            "giveback_pct": round(peak_pct, 4),
        })
    return trades


# ── 백테스트 실행 ────────────────────────────────────────────────────────────

def _compute_signals(fdf: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    """이동평균 + 신호 컬럼 계산 (느린 단계). 결과를 재사용해 시뮬레이션만 반복 가능."""
    sub = fdf[fdf["code"].isin(codes)].copy()
    sig = sub.groupby("code", group_keys=False).apply(_add_signals)
    valid = sig["MA120"].notna()
    uptrend_days  = sig.loc[valid, "full_uptrend"].sum()
    entry_signals = (sig.loc[valid, "in_uptrend"]
                     & sig.loc[valid, "is_min_body7"]
                     & sig.loc[valid, "is_closest_ma7"]).sum()
    print(f"  상승추세 달성일: {int(uptrend_days):,}건  |  기술적 진입 신호: {int(entry_signals):,}건")
    return sig


def _run_simulation(sig_df: pd.DataFrame, codes: list[str],
                    stop_loss: float, max_hold: int,
                    ssen_filter: dict | None = None,
                    macro_filter: dict | None = None,
                    label: str = "",
                    fixed_stop_pct: float | None = None,
                    bb_volatility: bool = False) -> pd.DataFrame:
    """사전 계산된 신호 DataFrame으로 시뮬레이션만 실행 (빠른 단계)."""
    all_trades: list[dict] = []
    for code in codes:
        sdf = sig_df[sig_df["code"] == code].reset_index(drop=True)
        if len(sdf) < 130:
            continue
        all_trades.extend(_simulate(sdf, stop_loss, max_hold, ssen_filter, macro_filter,
                                    fixed_stop_pct=fixed_stop_pct,
                                    bb_volatility=bb_volatility))
    trades_df = pd.DataFrame(all_trades)
    n_closed = len(trades_df[trades_df["exit_reason"] != "open"]) if not trades_df.empty else 0
    tag = f" [{label}]" if label else ""
    print(f"  총{tag} {len(trades_df)}건 (청산 {n_closed}건 / 미청산 {len(trades_df)-n_closed}건)")
    return trades_df


def run_backtest(df: pd.DataFrame, codes: list[str],
                 stop_loss: float, max_hold: int,
                 ssen_filter: dict | None = None,
                 macro_filter: dict | None = None) -> pd.DataFrame:
    step = "[3]"
    print(f"\n{step} 백테스트 실행 ({len(codes)}종목)...")
    print("  이동평균·신호 계산 중...")
    sig_df = _compute_signals(df, codes)
    trades_df = _run_simulation(sig_df, codes, stop_loss, max_hold, ssen_filter, macro_filter)
    return trades_df


# ── 거시 환경 필터 비교 분석 ─────────────────────────────────────────────────

def run_macro_comparison(df: pd.DataFrame, codes: list[str],
                         stop_loss: float, max_hold: int,
                         ssen_filter: dict | None,
                         sam_filter: dict | None,
                         adr_filter: dict | None,
                         bb_watch_filter: dict | None = None,
                         bb_confirm_filter: dict | None = None) -> dict[str, pd.DataFrame]:
    """
    신호 계산 1회 + 시뮬레이션 N회로 거시 필터별 성과를 비교.
    반환: {"기준": trades_df, "ADR폭": ..., "BB매수주시": ..., "BB매수확정": ...}
    """
    print("\n[M] 거시 환경 필터 비교 분석")
    print("  이동평균·신호 계산 중...")
    sig_df = _compute_signals(df, codes)

    results: dict[str, pd.DataFrame] = {}

    print("  시뮬레이션 실행:")
    results["기준(필터없음)"] = _run_simulation(
        sig_df, codes, stop_loss, max_hold, ssen_filter, None, "기준")

    if sam_filter is not None:
        results["삼성전자강도"] = _run_simulation(
            sig_df, codes, stop_loss, max_hold, ssen_filter, sam_filter, "삼성강도")

    if adr_filter is not None:
        results["ADR시장폭"] = _run_simulation(
            sig_df, codes, stop_loss, max_hold, ssen_filter, adr_filter, "ADR폭")

    if sam_filter is not None and adr_filter is not None:
        all_dates = set(sam_filter.keys()) | set(adr_filter.keys())
        combined = {dt: (sam_filter.get(dt, False) and adr_filter.get(dt, False))
                    for dt in all_dates}
        results["삼성+ADR결합"] = _run_simulation(
            sig_df, codes, stop_loss, max_hold, ssen_filter, combined, "삼성+ADR")

    if bb_watch_filter is not None:
        results["BB매수주시"] = _run_simulation(
            sig_df, codes, stop_loss, max_hold, ssen_filter, bb_watch_filter, "BB주시")

    if bb_confirm_filter is not None:
        results["BB매수확정"] = _run_simulation(
            sig_df, codes, stop_loss, max_hold, ssen_filter, bb_confirm_filter, "BB확정")

    if adr_filter is not None and bb_watch_filter is not None:
        all_dates = set(adr_filter.keys()) | set(bb_watch_filter.keys())
        combined2 = {dt: (adr_filter.get(dt, False) and bb_watch_filter.get(dt, False))
                     for dt in all_dates}
        results["ADR+BB결합"] = _run_simulation(
            sig_df, codes, stop_loss, max_hold, ssen_filter, combined2, "ADR+BB")

    return results


def _macro_stats(trades_df: pd.DataFrame) -> dict | None:
    """trades_df → 핵심 통계 dict. 청산 거래 없으면 None."""
    if trades_df.empty:
        return None
    closed = trades_df[trades_df["exit_reason"] != "open"]
    if closed.empty:
        return None
    pnls  = closed["net_pnl_pct"].dropna()
    total = len(pnls)
    if total == 0:
        return None
    wins   = int((pnls > 0).sum())
    cumret = (1 + pnls / 100).cumprod()
    mdd    = float(((cumret - cumret.cummax()) / cumret.cummax() * 100).min())
    std    = float(pnls.std())
    sharpe = float(pnls.mean() / std * (252 ** 0.5)) if std > 0 else 0.0
    return {
        "total": total, "wins": wins,
        "avg": float(pnls.mean()), "med": float(pnls.median()),
        "sharpe": sharpe, "cum": float((cumret.iloc[-1] - 1) * 100),
        "mdd": mdd,
        "stop_loss": int((closed["exit_reason"] == "stop_loss").sum()),
        "closed": closed,
    }


def print_macro_comparison(results: dict[str, pd.DataFrame],
                           sam_window: int, adr_window: int, adr_thresh: float) -> None:
    W   = 90
    sep = "=" * W
    print(f"\n{sep}")
    print(f"  거시 환경 필터 비교 분석")
    print(f"  삼성전자 상대강도: 최근 {sam_window}거래일 누적 수익률 삼성전자 ≥ KOSPI 시총가중")
    print(f"  ADR 시장폭: 최근 {adr_window}거래일 평균 ADR ≥ {adr_thresh} (상승종목비율)")
    print(f"  BB매수주시: BR5>0.55 + McClellan>0 + 순상승>0 중 2개 이상 충족")
    print(f"  BB매수확정: 위 조건을 3일 연속 충족")
    print(sep)

    hdr = (f"  {'필터':<14}  {'거래':>5}  {'승률':>6}  "
           f"{'평균%':>7}  {'Sharpe':>7}  {'누적%':>8}  {'MDD%':>8}  {'손절':>4}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    base_sharpe: float | None = None
    for label, trades_df in results.items():
        s = _macro_stats(trades_df)
        if s is None:
            print(f"  {label:<14}  (거래없음)")
            continue

        delta = ""
        if base_sharpe is None:
            base_sharpe = s["sharpe"]
        else:
            delta = f"({s['sharpe'] - base_sharpe:+.2f})"

        print(f"  {label:<14}  {s['total']:>5}건  "
              f"{s['wins'] / s['total'] * 100:>5.1f}%  "
              f"{s['avg']:>+6.2f}%  "
              f"{s['sharpe']:>7.2f}{delta:<7}  "
              f"{s['cum']:>+7.1f}%  "
              f"{s['mdd']:>+7.1f}%  "
              f"{s['stop_loss']:>4}건")

    # ── 연도별 승률·건수 비교 ─────────────────────────────────────────────────
    print(f"\n  ── 연도별 비교 (건수 / 승률%) ──")
    labels = list(results.keys())
    hdr2 = f"  {'연도':>4}"
    for lbl in labels:
        hdr2 += f"  {lbl[:8]:>12}"
    print(hdr2)
    print("  " + "-" * (len(hdr2) - 2))

    all_years: set[int] = set()
    for trades_df in results.values():
        if trades_df.empty:
            continue
        closed = trades_df[trades_df["exit_reason"] != "open"]
        if closed.empty:
            continue
        yrs = pd.to_datetime(closed["entry_date"].astype(str)).dt.year
        all_years.update(yrs.dropna().astype(int).tolist())

    for yr in sorted(all_years):
        row = f"  {yr:>4}"
        for lbl in labels:
            trades_df = results[lbl]
            if trades_df.empty:
                row += f"  {'N/A':>12}"
                continue
            closed = trades_df[trades_df["exit_reason"] != "open"]
            if closed.empty:
                row += f"  {'N/A':>12}"
                continue
            yr_mask = pd.to_datetime(closed["entry_date"].astype(str)).dt.year == yr
            yr_pnls = closed.loc[yr_mask, "net_pnl_pct"].dropna()
            if len(yr_pnls) == 0:
                row += f"  {'  -':>12}"
            else:
                wr = (yr_pnls > 0).mean() * 100
                row += f"  {len(yr_pnls):>4}건/{wr:>5.1f}%"
        print(row)

    print(sep)

    # ── 필터 효과 해설 ────────────────────────────────────────────────────────
    all_stats = {lbl: _macro_stats(df) for lbl, df in results.items()}
    base_s = all_stats.get("기준(필터없음)")
    if base_s is None:
        return

    print(f"\n  ── 필터 효과 해설 ──")
    for lbl, s in all_stats.items():
        if lbl == "기준(필터없음)" or s is None:
            continue
        trade_red = (base_s["total"] - s["total"]) / base_s["total"] * 100
        win_delta = s["wins"] / s["total"] * 100 - base_s["wins"] / base_s["total"] * 100
        sharpe_delta = s["sharpe"] - base_s["sharpe"]
        mdd_delta    = s["mdd"] - base_s["mdd"]
        print(f"  [{lbl}]")
        print(f"    진입 감소 : {trade_red:+.1f}%  ({base_s['total']}건 → {s['total']}건)")
        print(f"    승률 변화 : {win_delta:+.1f}%p")
        print(f"    Sharpe    : {base_s['sharpe']:.2f} → {s['sharpe']:.2f} ({sharpe_delta:+.2f})")
        print(f"    MDD 변화  : {base_s['mdd']:.1f}% → {s['mdd']:.1f}% ({mdd_delta:+.1f}%p)")
    print(sep)


# ── ADR 임계값 sweep ─────────────────────────────────────────────────────────

def run_adr_sweep(df: pd.DataFrame, codes: list[str],
                  stop_loss: float, max_hold: int,
                  ssen_filter: dict | None,
                  windows: list[int],
                  thresholds: list[float]) -> dict:
    """
    신호 계산 1회 + (window × thresh + 1 baseline) 번 시뮬레이션.
    반환: {
      "baseline": trades_df,
      (window, thresh): trades_df, ...
    }
    """
    print(f"\n[SWEEP] ADR 임계값 최적화 ({len(windows)}×{len(thresholds)} = "
          f"{len(windows)*len(thresholds)}조합 + baseline)")
    print("  이동평균·신호 계산 중...")
    sig_df = _compute_signals(df, codes)

    # 베이스라인
    print("  시뮬레이션:")
    results: dict = {}
    results["baseline"] = _run_simulation(
        sig_df, codes, stop_loss, max_hold, ssen_filter, None, "baseline")

    # 모든 ADR 필터를 한 번에 빌드 (OHLCV 1회 로드)
    filter_matrix = build_adr_filter_multi(windows, thresholds)

    total = len(windows) * len(thresholds)
    done  = 0
    for window in windows:
        for thresh in thresholds:
            done += 1
            key    = (window, thresh)
            filt   = filter_matrix[key]
            label  = f"w{window}/t{thresh:.2f}"
            trades = _run_simulation(
                sig_df, codes, stop_loss, max_hold, ssen_filter, filt, label)
            results[key] = trades
            if done % 8 == 0 or done == total:
                print(f"    진행: {done}/{total}")

    return results


def print_adr_sweep(results: dict,
                    windows: list[int],
                    thresholds: list[float]) -> None:
    """
    Sharpe 히트맵 테이블 + 상위 20개 조합 랭킹 출력.
    기준 대비 개선된 셀에 * 표시.
    """
    W   = 82
    sep = "=" * W

    def s(key) -> dict | None:
        return _macro_stats(results.get(key, pd.DataFrame()))

    base = s("baseline")
    if base is None:
        print("  (baseline 거래 없음)")
        return
    base_sharpe = base["sharpe"]
    base_cum    = base["cum"]
    base_mdd    = base["mdd"]

    # ── Sharpe 히트맵 ─────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  ADR 임계값 최적화 — Sharpe 히트맵  (기준 baseline: {base_sharpe:.2f})")
    print(f"  * = baseline({base_sharpe:.2f}) 초과  / 셀 형식: Sharpe (거래건수)")
    print(sep)

    col_w = 13
    hdr   = f"  {'window\\thresh':>12}"
    for t in thresholds:
        hdr += f"  {t:.2f}".rjust(col_w)
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for window in windows:
        row = f"  {f'{window}일':>12}"
        for thresh in thresholds:
            st = s((window, thresh))
            if st is None:
                row += f"  {'N/A':>{col_w}}"
                continue
            marker = "*" if st["sharpe"] > base_sharpe else " "
            cell   = f"{st['sharpe']:.2f}({st['total']:>3}){marker}"
            row   += f"  {cell:>{col_w}}"
        print(row)

    # ── 누적수익 히트맵 ───────────────────────────────────────────────────────
    print(f"\n  ADR 임계값 최적화 — 누적수익% 히트맵  (기준: {base_cum:+.1f}%)")
    print(f"  * = baseline 초과")
    print("  " + "-" * (len(hdr) - 2))
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for window in windows:
        row = f"  {f'{window}일':>12}"
        for thresh in thresholds:
            st = s((window, thresh))
            if st is None:
                row += f"  {'N/A':>{col_w}}"
                continue
            marker = "*" if st["cum"] > base_cum else " "
            cell   = f"{st['cum']:+.1f}%({st['total']:>3}){marker}"
            row   += f"  {cell:>{col_w}}"
        print(row)

    # ── MDD 히트맵 ────────────────────────────────────────────────────────────
    print(f"\n  ADR 임계값 최적화 — MDD% 히트맵  (기준: {base_mdd:.1f}%)  * = 개선(MDD 감소)")
    print("  " + "-" * (len(hdr) - 2))
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for window in windows:
        row = f"  {f'{window}일':>12}"
        for thresh in thresholds:
            st = s((window, thresh))
            if st is None:
                row += f"  {'N/A':>{col_w}}"
                continue
            marker = "*" if st["mdd"] > base_mdd else " "
            cell   = f"{st['mdd']:.1f}%({st['total']:>3}){marker}"
            row   += f"  {cell:>{col_w}}"
        print(row)

    # ── 상위 20개 조합 랭킹 ───────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  TOP 20 — Sharpe 기준 정렬 (baseline: Sharpe {base_sharpe:.2f} / "
          f"누적 {base_cum:+.1f}% / MDD {base_mdd:.1f}%)")
    print(sep)
    hdr2 = (f"  {'순위':>4}  {'window':>6}  {'thresh':>6}  {'거래':>5}  {'승률':>6}  "
            f"{'Sharpe':>7}  {'누적%':>8}  {'MDD%':>8}  {'평균%':>7}  {'손절':>4}")
    print(hdr2)
    print("  " + "-" * (len(hdr2) - 2))

    # 수집 후 Sharpe 정렬
    rows: list[dict] = []
    for window in windows:
        for thresh in thresholds:
            st = s((window, thresh))
            if st is None:
                continue
            rows.append({"window": window, "thresh": thresh, **st})
    rows.sort(key=lambda r: r["sharpe"], reverse=True)

    for rank, r in enumerate(rows[:20], 1):
        delta_sh  = r["sharpe"] - base_sharpe
        mark_sh   = "▲" if delta_sh > 0 else "▼"
        print(f"  {rank:>4}  {r['window']:>6}일  {r['thresh']:>6.2f}  "
              f"{r['total']:>5}건  "
              f"{r['wins']/r['total']*100:>5.1f}%  "
              f"{r['sharpe']:>7.2f}{mark_sh}({delta_sh:+.2f})  "
              f"{r['cum']:>+7.1f}%  "
              f"{r['mdd']:>+7.1f}%  "
              f"{r['avg']:>+6.2f}%  "
              f"{r['stop_loss']:>4}건")

    # ── 연도별 성과 (상위 3개 조합) ───────────────────────────────────────────
    print(f"\n  ── 상위 3개 조합 연도별 성과 ──")
    top3_keys = [("baseline", None, None)] + [
        ((r["window"], r["thresh"]), r["window"], r["thresh"])
        for r in rows[:3]
    ]
    yearly_hdr = f"  {'연도':>4}  {'baseline':>12}"
    for _, w, t in top3_keys[1:]:
        yearly_hdr += f"  {f'w{w}/t{t:.2f}':>14}"
    print(yearly_hdr)
    print("  " + "-" * (len(yearly_hdr) - 2))

    all_years: set[int] = set()
    for key, _, _ in top3_keys:
        td = results.get(key, pd.DataFrame())
        if td.empty:
            continue
        cl = td[td["exit_reason"] != "open"]
        if cl.empty:
            continue
        all_years.update(
            pd.to_datetime(cl["entry_date"].astype(str)).dt.year.dropna().astype(int).tolist()
        )

    for yr in sorted(all_years):
        row_str = f"  {yr:>4}"
        for key, _, _ in top3_keys:
            td = results.get(key, pd.DataFrame())
            if td.empty:
                row_str += f"  {'N/A':>14}"
                continue
            cl = td[td["exit_reason"] != "open"]
            if cl.empty:
                row_str += f"  {'N/A':>14}"
                continue
            yr_mask = pd.to_datetime(cl["entry_date"].astype(str)).dt.year == yr
            pnls    = cl.loc[yr_mask, "net_pnl_pct"].dropna()
            if len(pnls) == 0:
                row_str += f"  {'  -':>14}"
            else:
                wr = (pnls > 0).mean() * 100
                avg = pnls.mean()
                row_str += f"  {len(pnls):>3}건/{wr:>5.1f}%/{avg:>+5.1f}%"
        print(row_str)

    print(sep)


# ── 리포트 ───────────────────────────────────────────────────────────────────

def print_report(trades_df: pd.DataFrame, stop_loss: float, max_hold: int) -> None:
    if trades_df.empty:
        print("\n거래 없음 — 조건을 충족한 신호가 없습니다.")
        print("  Tip: --no-filter 또는 --ratio 3 으로 유니버스를 넓혀보세요.")
        return

    closed = trades_df[trades_df["exit_reason"] != "open"].copy()
    open_  = trades_df[trades_df["exit_reason"] == "open"]

    if closed.empty:
        print(f"\n청산 거래 없음 (미청산 {len(open_)}건)")
        return

    pnls  = closed["net_pnl_pct"].dropna()
    wins  = (pnls > 0).sum()
    total = len(pnls)
    cumret = (1 + pnls / 100).cumprod()
    mdd    = ((cumret - cumret.cummax()) / cumret.cummax() * 100).min()
    std    = pnls.std()
    sharpe = pnls.mean() / std * (252 ** 0.5) if std > 0 else 0

    W = 60
    sep = "=" * W
    print(f"\n{sep}")
    print(f"  눌림목 백테스트 v3")
    print(sep)
    print(f"  기간           : {START_DATE} ~ {END_DATE}")
    print(f"  상승추세 1     : close > MA5 > MA10 > MA20 > MA60 > MA120")
    print(f"  상승추세 2     : MA5 > 과거 240거래일 MA5 최고값 (52주 신고가)")
    print(f"  상승추세 3     : MA10/MA60 < 1.05  (세 선 5% 이내 밀집)")
    print(f"  상승추세 유효  : 위 조건이 최근 {UPTREND_LOOKBACK}봉 내 충족")
    print(f"  눌림목 진입    : 최근7봉 중 몸통 최소 AND MA7 최근접 동시 만족 → 당일 종가 매수")
    print(f"  청산           : 종가 < MA7  |  안전손절 -{stop_loss}%  |  {max_hold}일")
    print(f"  수수료         : {FEE_PCT}%")
    print(sep)
    print(f"  청산 거래      : {total}건  (미청산 {len(open_)}건)")
    print(f"  승률           : {wins/total*100:.1f}%  ({wins}승 {total-wins}패)")
    print(f"  평균 수익률    : {pnls.mean():.2f}%")
    print(f"  중앙값         : {pnls.median():.2f}%")
    print(f"  표준편차       : {std:.2f}%")
    print(f"  Sharpe         : {sharpe:.2f}")
    print(f"  누적 수익률    : {(cumret.iloc[-1]-1)*100:.2f}%")
    print(f"  MDD            : {mdd:.2f}%")
    print(f"  평균 보유일    : {closed['hold_days'].mean():.1f}거래일")

    reason_map = {
        "ma7_break":"MA7이탈", "stop_loss":"안전손절", "time_stop":"시간청산"
    }
    print(f"\n  ── 청산 사유 ──")
    for r, cnt in closed["exit_reason"].value_counts().items():
        lbl = reason_map.get(r, r)
        avg = closed[closed["exit_reason"]==r]["net_pnl_pct"].mean()
        print(f"    {lbl:<10} : {cnt:>5}건 ({cnt/total*100:5.1f}%)  평균 {avg:+.2f}%")

    print(f"\n  ── 수익률 분포 ──")
    bins   = [-999, -20, -10, -5, 0, 5, 10, 20, 50, 999]
    labels = ["<-20%","-20~-10%","-10~-5%","-5~0%",
              "0~+5%","+5~+10%","+10~+20%","+20~+50%",">+50%"]
    cut = pd.cut(pnls, bins=bins, labels=labels)
    for lbl, cnt in cut.value_counts().sort_index().items():
        bar = "█" * int(cnt / max(1, total) * 40)
        print(f"    {lbl:>10} : {cnt:>5}건  {bar}")

    # 성과 상위 종목
    stock_perf = (
        closed.groupby(["code","name"])
        .agg(cnt=("net_pnl_pct","count"),
             avg=("net_pnl_pct","mean"),
             wr=("net_pnl_pct", lambda x: (x>0).mean()*100),
             total_pnl=("net_pnl_pct","sum"))
        .reset_index()
    )
    print(f"\n  ── 평균 수익률 상위 종목 (≥2건) ──")
    top_avg = stock_perf[stock_perf["cnt"] >= 2].sort_values("avg", ascending=False).head(10)
    for _, r in top_avg.iterrows():
        print(f"    {r['code']} {str(r['name'])[:12]:<12} "
              f"{r['cnt']:>3}건  평균{r['avg']:+6.1f}%  승률{r['wr']:.0f}%")

    print(f"\n  ── 거래 빈도 상위 종목 ──")
    top_cnt = stock_perf.sort_values("cnt", ascending=False).head(10)
    for _, r in top_cnt.iterrows():
        print(f"    {r['code']} {str(r['name'])[:12]:<12} "
              f"{r['cnt']:>3}건  평균{r['avg']:+6.1f}%  승률{r['wr']:.0f}%")

    # 연도별 성과
    closed2 = closed.copy()
    closed2["year"] = pd.to_datetime(closed2["entry_date"].astype(str)).dt.year
    yearly = (closed2.groupby("year")
              .agg(cnt=("net_pnl_pct","count"),
                   avg=("net_pnl_pct","mean"),
                   wr=("net_pnl_pct", lambda x: (x>0).mean()*100))
              .reset_index())
    print(f"\n  ── 연도별 성과 ──")
    for _, r in yearly.iterrows():
        bar = "█" * int(max(0, r['avg']) * 2)
        print(f"    {int(r['year'])}  {r['cnt']:>5}건  평균{r['avg']:+6.2f}%  "
              f"승률{r['wr']:4.1f}%  {bar}")

    # ── MFE 반납 분석 ──────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  MFE 반납 분석 — 보유 중 최고 수익(peak) vs 실제 청산 수익")
    print(sep)

    # 구간별 요약
    mfe_bins    = [-999, -20, -10, -5, 0, 5, 10, 20, 50, 999]
    mfe_buckets = ["<-20%","-20~-10%","-10~-5%","-5~0%",
                   "0~+5%","+5~+10%","+10~+20%","+20~+50%",">+50%"]
    closed["_bucket"] = pd.cut(closed["net_pnl_pct"], bins=mfe_bins, labels=mfe_buckets)

    hdr = (f"  {'청산구간':>10}  {'건수':>4}  "
           f"{'avg 최고%':>9}  {'avg 청산%':>9}  {'avg 반납%':>9}  "
           f"{'최대반납%':>9}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for lbl in mfe_buckets:
        grp = closed[closed["_bucket"] == lbl]
        if grp.empty:
            continue
        n     = len(grp)
        a_pk  = grp["peak_pct"].mean()
        a_ex  = grp["net_pnl_pct"].mean()
        a_gb  = grp["giveback_pct"].mean()
        mx_gb = grp["giveback_pct"].max()
        print(f"  {lbl:>10}  {n:>4}건  "
              f"{a_pk:>+8.2f}%  {a_ex:>+8.2f}%  {a_gb:>+8.2f}%  {mx_gb:>+8.2f}%")

    # 고수익 거래 상세표 (청산 수익 ≥ +5%)
    high = closed[closed["net_pnl_pct"] >= 5.0].sort_values("net_pnl_pct", ascending=False)
    if not high.empty:
        print(f"\n  ── 청산 수익 ≥ +5% 거래 상세 ({len(high)}건) ──")
        hdr2 = (f"  {'종목명':<12}  {'진입일':>10}  {'최고일':>10}  {'청산일':>10}  "
                f"{'진입가':>7}  {'최고가':>7}  {'청산가':>7}  "
                f"{'최고%':>7}  {'청산%':>7}  {'반납%':>7}  {'일수':>4}")
        print(hdr2)
        print("  " + "-" * (len(hdr2) - 2))
        for _, r in high.iterrows():
            nm = str(r["name"])[:11]
            print(f"  {nm:<12}  {str(r['entry_date']):>10}  "
                  f"{str(r['peak_date']):>10}  {str(r['exit_date']):>10}  "
                  f"{int(r['entry_price']):>7,}  {int(r['peak_price']):>7,}  "
                  f"{int(r['exit_price']):>7,}  "
                  f"{r['peak_pct']:>+6.1f}%  {r['net_pnl_pct']:>+6.1f}%  "
                  f"{-r['giveback_pct']:>+6.1f}%  {int(r['hold_days']):>4}일")

    print(f"\n{sep}")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="눌림목 백테스트 v3")
    parser.add_argument("--ratio",     type=float, default=RATIO_THRESHOLD)
    parser.add_argument("--no-filter", action="store_true", help="전 종목 실행")
    parser.add_argument("--buffett",   action="store_true",
                        help="버핏형 국내 종목(buffett_kr_200_v2.xlsx)으로 유니버스 제한")
    parser.add_argument("--ssen",      choices=["theme","leader","rank"], default=None,
                        help="SSen 주도주 필터  theme=주도 테마  leader=주도주  rank=거래대금 상위N")
    parser.add_argument("--ssen-window",   type=int, default=20,
                        help="SSen 롤링 윈도우 거래일 (기본 20)")
    parser.add_argument("--ssen-rank-top", type=int, default=50,
                        help="rank 모드 상위 N위 (기본 50)")
    parser.add_argument("--stop",      type=float, default=STOP_LOSS_PCT,
                        help="안전 손절 %% (기본 15)")
    parser.add_argument("--max-hold",  type=int,   default=MAX_HOLD_DAYS,
                        help="최대 보유 거래일 (기본 120)")
    # ── 거시 환경 필터 ──
    parser.add_argument("--macro-sam",        action="store_true",
                        help="삼성전자 vs KOSPI 상대강도 필터")
    parser.add_argument("--macro-sam-window", type=int, default=20,
                        help="삼성전자 상대강도 rolling window 거래일 (기본 20)")
    parser.add_argument("--macro-adr",        action="store_true",
                        help="ADR(상승종목비율) 시장 폭 필터")
    parser.add_argument("--macro-adr-window", type=int,   default=10,
                        help="ADR rolling window 거래일 (기본 10)")
    parser.add_argument("--macro-adr-thresh", type=float, default=0.45,
                        help="ADR 최소 임계값 (기본 0.45)")
    parser.add_argument("--macro-bullbear",   action="store_true",
                        help="BullBear 3중확인 필터 (BR₅+McClellan+A/D Line)")
    parser.add_argument("--macro-bullbear-mode", choices=["watch", "confirm", "both"],
                        default="both",
                        help="watch=매수주시(N>=2), confirm=매수확정(3일연속), both=둘 다 (기본 both)")
    # ── ADR sweep ──
    parser.add_argument("--macro-adr-sweep",  action="store_true",
                        help="ADR window×thresh 전 조합 sweep (최적화)")
    parser.add_argument("--macro-adr-sweep-windows",
                        type=str, default="5,10,15,20",
                        help="sweep할 window 목록, 콤마 구분 (기본 5,10,15,20)")
    parser.add_argument("--macro-adr-sweep-thresholds",
                        type=str, default="0.35,0.40,0.42,0.44,0.46,0.48,0.50,0.52,0.55",
                        help="sweep할 thresh 목록, 콤마 구분")
    args = parser.parse_args()
    if args.no_filter:
        args.ratio = 0

    t0 = time.time()
    df = load_ohlcv()

    # SSen 주도주 필터 (해당 옵션 선택 시 [2]단계 로드)
    ssen_filter = (load_ssen_leader_filter(args.ssen,
                                           window=args.ssen_window,
                                           rank_top=args.ssen_rank_top)
                   if args.ssen else None)

    fdf, codes = filter_universe(df, args.ratio, buffett=args.buffett)

    # ── ADR sweep 모드 ────────────────────────────────────────────────────────
    if args.macro_adr_sweep:
        sweep_windows    = [int(x) for x in args.macro_adr_sweep_windows.split(",")]
        sweep_thresholds = [float(x) for x in args.macro_adr_sweep_thresholds.split(",")]
        sweep_results = run_adr_sweep(fdf, codes, args.stop, args.max_hold,
                                      ssen_filter, sweep_windows, sweep_thresholds)
        print_adr_sweep(sweep_results, sweep_windows, sweep_thresholds)
        print(f"\n  실행 시간: {time.time()-t0:.1f}초")
        return

    # ── 거시 환경 필터 비교 모드 ──────────────────────────────────────────────
    if args.macro_sam or args.macro_adr or args.macro_bullbear:
        sam_filter = (build_samsung_kospi_filter(window=args.macro_sam_window)
                      if args.macro_sam else None)
        adr_filter = (build_adr_filter(window=args.macro_adr_window,
                                       thresh=args.macro_adr_thresh)
                      if args.macro_adr else None)
        bb_watch_filter   = None
        bb_confirm_filter = None
        if args.macro_bullbear:
            mode = args.macro_bullbear_mode
            if mode in ("watch", "both"):
                bb_watch_filter   = build_bullbear_filter(mode="watch")
            if mode in ("confirm", "both"):
                bb_confirm_filter = build_bullbear_filter(mode="confirm")
        results = run_macro_comparison(fdf, codes, args.stop, args.max_hold,
                                       ssen_filter, sam_filter, adr_filter,
                                       bb_watch_filter, bb_confirm_filter)
        print_macro_comparison(results,
                               sam_window=args.macro_sam_window,
                               adr_window=args.macro_adr_window,
                               adr_thresh=args.macro_adr_thresh)
        # 기준 결과도 상세 리포트로 출력
        print_report(results["기준(필터없음)"], args.stop, args.max_hold)
    else:
        trades_df = run_backtest(fdf, codes, args.stop, args.max_hold,
                                 ssen_filter=ssen_filter)
        print_report(trades_df, args.stop, args.max_hold)

    print(f"\n  실행 시간: {time.time()-t0:.1f}초")


if __name__ == "__main__":
    main()

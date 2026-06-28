# -*- coding: utf-8 -*-
"""E9: 일일 데이터 수집·백필 파이프라인.

3개 데이터 소스별 키움/data.go.kr 역할이 서로 다르다 (2026-06-17 실측 검증, KOA Studio
대신 pykiwoom 실거래 호출로 직접 확인 — docs/kiwoom_collection_spec.md 참조):

  거래대금 (opt10032)   : 키움은 "당일"만 가능(실시간 조회) → 실패 시 data.go.kr 폴백
  코스피시세 (opt20006) : 키움이 "당일+과거 백필 모두" 가능(차트형 TR) → 실패 시 data.go.kr 폴백
  ADR (opt20009)        : 키움은 "당일"만 가능, 과거 날짜 조회 TR이 키움에 존재하지 않음
                          → 과거 백필은 처음부터 data.go.kr 전용(폴백이 아니라 역할 분리),
                            당일은 키움만 시도하고 실패해도 data.go.kr로 대체하지 않음
                          (data.go.kr도 1~2일 지연이라 당일 데이터가 구조적으로 없기 때문)

기존 월 파티션과의 머지는 docs/api_collection.md의 upsert 패턴을 따른다:
    combined = pd.concat([existing, new]).drop_duplicates(subset=key_cols, keep='last')
신규 1일치만 담은 xlsx를 바로 ingest하면 convert_excel_to_parquet.py의 overlap=rebuild
정책이 해당 월 파티션을 "이 파일에 있는 행만"으로 덮어써 기존 데이터가 손실되므로,
반드시 기존 월 데이터 + 신규 데이터를 합친 "완전한 월" 단위로 incoming xlsx를 만든다.

Assumptions (clarifying question 금지 규칙에 따름):
  - 영업일 = 토/일 제외 평일 (공휴일 캘린더 미연동, 기존 SSen_Money.py와 동일 가정)
  - 거래대금/ADR의 키움 경로는 date.today()와 target_date가 같을 때만 시도
    (코스피시세는 차트형 TR이라 과거 날짜에도 동일하게 시도)
  - 32bit 키움 실행기는 `py -3.9-32`로 가정 (docs/kiwoom_collection_spec.md 환경 구성 기록)
  - ADR 키움 수집 시각은 verify_adr_timing.py 검증 결과 확정 전까지 잠정적으로 "장마감 직후"
    (배치 실행 시점에 따름, 별도 시각 제어 없음) — 검증 후 필요 시 스케줄 조정
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

PARQUET_DIR = ROOT / "data" / "parquet"
INCOMING_DIR = ROOT / "data" / "incoming"
KIWOOM_DIR = ROOT / "src" / "ssen" / "collect" / "kiwoom"
KIWOOM_SCRIPT = KIWOOM_DIR / "collect_kiwoom_money.py"
KIWOOM_KOSPI_SCRIPT = KIWOOM_DIR / "collect_kiwoom_kospi.py"
KIWOOM_ADR_SCRIPT = KIWOOM_DIR / "collect_kiwoom_adr.py"
KIWOOM_OHLCV_SCRIPT = KIWOOM_DIR / "collect_kiwoom_ohlcv.py"
KIWOOM_OUTPUT_DIR = ROOT / "data" / "incoming" / "kiwoom"
KIWOOM_PYTHON = "py"
KIWOOM_PYTHON_ARGS = ["-3.9-32"]
KIWOOM_TIMEOUT_SEC = 180
KIWOOM_OHLCV_TIMEOUT_SEC = 1800  # universe 1000+종목 × (0.45s 호출+0.3s 슬립) ≈ 15분대 실측

MARKET_OHLCV_DIR = ROOT / "data" / "market" / "ohlcv"
OHLCV_UNIVERSE_LOOKBACK_TRADING_DAYS = 250  # RS_PERIOD_12M=250과 정의 일치

STOCK_COLS = ["날짜", "순위", "종목코드", "종목명", "시장구분", "시작일기준가", "종료일종가",
              "대비", "등락률", "거래량_합계", "거래량_일평균", "거래대금_합계", "거래대금_일평균"]


# ── 영업일 계산 ────────────────────────────────────────────────────────────────

def missing_business_days(today: Optional[date] = None) -> list[date]:
    """manifest max_date+1 ~ today 사이의 누락 영업일 목록."""
    from ssen.pipeline.state import get_manifest_max_date
    today = today or date.today()
    prev_max = get_manifest_max_date()
    start = (prev_max + timedelta(days=1)) if prev_max else today
    if start > today:
        return []
    return [d.date() for d in pd.bdate_range(start, today)]


# ── 거래대금: 키움 경로 ──────────────────────────────────────────────────────────

def collect_stock_kiwoom(target_date: date) -> Optional[pd.DataFrame]:
    """키움 32bit 서브프로세스 호출. target_date가 오늘이 아니면 즉시 None (과거 백필 불가)."""
    if target_date != date.today():
        return None
    KIWOOM_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [KIWOOM_PYTHON, *KIWOOM_PYTHON_ARGS, str(KIWOOM_SCRIPT)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=KIWOOM_TIMEOUT_SEC)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        print(f"  [키움] 서브프로세스 실행 실패: {e}")
        return None
    if proc.returncode != 0:
        print(f"  [키움] 종료코드 {proc.returncode}: {proc.stderr.strip()[:300]}")
        return None
    out_path = KIWOOM_OUTPUT_DIR / f"kiwoom_money_{target_date.strftime('%Y%m%d')}.csv"
    if not out_path.exists():
        print(f"  [키움] 출력 파일 없음: {out_path}")
        return None
    df = pd.read_csv(out_path, encoding="utf-8-sig", dtype={"종목코드": str})
    if df.empty:
        return None
    print(f"  [키움] 수집 성공: {len(df)}행")
    return df[STOCK_COLS]


# ── 코스피시세: 키움 경로 (당일+과거 백필 모두) ───────────────────────────────────

def collect_kospi_kiwoom(start: date, end: date) -> pd.DataFrame:
    """OPT20006 — 차트형 TR이라 당일/과거 구분 없이 동일 경로로 호출."""
    KIWOOM_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [KIWOOM_PYTHON, *KIWOOM_PYTHON_ARGS, str(KIWOOM_KOSPI_SCRIPT),
           "--start", str(start), "--end", str(end)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=KIWOOM_TIMEOUT_SEC)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        print(f"  [키움] 코스피시세 서브프로세스 실행 실패: {e}")
        return pd.DataFrame()
    if proc.returncode != 0:
        print(f"  [키움] 코스피시세 종료코드 {proc.returncode}: {proc.stderr.strip()[:300]}")
        return pd.DataFrame()
    out_path = KIWOOM_OUTPUT_DIR / f"kiwoom_kospi_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.csv"
    if not out_path.exists():
        print(f"  [키움] 코스피시세 출력 파일 없음: {out_path}")
        return pd.DataFrame()
    df = pd.read_csv(out_path, encoding="utf-8-sig")
    if df.empty:
        return df
    print(f"  [키움] 코스피시세 수집 성공: {len(df)}행")
    return df


# ── ADR: 키움 경로 (당일 전용, 과거 백필은 담당하지 않음) ───────────────────────────

def collect_adr_kiwoom(target_date: date) -> pd.DataFrame:
    """OPT20009 — 당일 스냅샷만 제공. target_date != 오늘이면 호출 자체를 시도하지 않음."""
    if target_date != date.today():
        return pd.DataFrame()
    KIWOOM_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [KIWOOM_PYTHON, *KIWOOM_PYTHON_ARGS, str(KIWOOM_ADR_SCRIPT),
           "--date", str(target_date)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=KIWOOM_TIMEOUT_SEC)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        print(f"  [키움] ADR 서브프로세스 실행 실패: {e}")
        return pd.DataFrame()
    if proc.returncode != 0:
        print(f"  [키움] ADR 종료코드 {proc.returncode}: {proc.stderr.strip()[:300]}")
        return pd.DataFrame()
    out_path = KIWOOM_OUTPUT_DIR / f"kiwoom_adr_{target_date.strftime('%Y%m%d')}.csv"
    if not out_path.exists():
        print(f"  [키움] ADR 출력 파일 없음: {out_path}")
        return pd.DataFrame()
    df = pd.read_csv(out_path, encoding="utf-8-sig")
    if df.empty:
        return df
    print(f"  [키움] ADR 수집 성공: {len(df)}행")
    return df


# ── 거래대금: data.go.kr 폴백/백필 경로 ──────────────────────────────────────────

def collect_stock_datagokr(target_date: date) -> pd.DataFrame:
    """SSen_Money.py 로직 재사용 — 단일 날짜, KOSPI+KOSDAQ top50."""
    from ssen.collect.SSen_Money import fetch_all_by_date, to_top50_format
    yyyymmdd = target_date.strftime("%Y%m%d")
    frames = []
    for market in ["KOSPI", "KOSDAQ"]:
        raw = fetch_all_by_date(yyyymmdd, market)
        if raw.empty:
            continue
        top50 = to_top50_format(raw, yyyymmdd, market)
        if not top50.empty:
            frames.append(top50)
    if not frames:
        return pd.DataFrame(columns=STOCK_COLS)
    df = pd.concat(frames, ignore_index=True)
    df["날짜"] = df["날짜"].astype(str)
    print(f"  [data.go.kr] {target_date} 수집: {len(df)}행")
    return df[STOCK_COLS]


def collect_kospi_datagokr(start: date, end: date) -> pd.DataFrame:
    """코스피지수_시세.py 로직 재사용 — '코스피' 지수만 (거래대금 시트 스키마와 일치)."""
    from ssen.collect.코스피지수_시세 import fetch_index_prices, load_service_key
    key = load_service_key()
    df = fetch_index_prices(key, "코스피", start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if df.empty:
        return df
    df = df.rename(columns={"전일대비": "change_raw"})
    return df[["Date", "Open", "High", "Low", "Close", "Volume", "Increase rate"]]


def collect_adr_datagokr(start: date, end: date) -> pd.DataFrame:
    """지수_상승종목수.py 로직 재사용. data.go.kr은 항상 확정치이므로 is_verified=True."""
    from ssen.collect.지수_상승종목수 import get_market_adv_dec_stats, DATA_GO_KR_SERVICE_KEY, URL
    df = get_market_adv_dec_stats(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
                                   DATA_GO_KR_SERVICE_KEY, URL)
    if not df.empty:
        df["is_verified"] = True
    return df


# ── OHLCV: 키움 경로 (거래대금 상위 1년 누적 universe만) ──────────────────────────

def compute_ohlcv_universe(lookback_trading_days: int = OHLCV_UNIVERSE_LOOKBACK_TRADING_DAYS) -> list[str]:
    """fact_daily_stock에서 최근 N거래일 동안 한 번이라도 거래대금 상위에 든 종목코드.

    RS_breakout_strategy.py의 RS_PERIOD_12M(250거래일)과 정의를 맞춤 — RS 계산에
    필요한 모집단을 좁히지 않으면서 매일 OHLCV 갱신 대상을 한정하기 위함.
    """
    today = date.today()
    months = sorted({(today - timedelta(days=30 * i)).strftime("%Y%m") for i in range(14)})
    frames = []
    for ym in months:
        p = PARQUET_DIR / "fact_daily_stock" / f"yearmonth={ym}" / "data.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p, columns=["date", "code"]))
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    dates = sorted(df["date"].astype(str).unique())
    recent_dates = dates[-lookback_trading_days:]
    return sorted(df[df["date"].astype(str).isin(recent_dates)]["code"].unique().tolist())


def collect_ohlcv_kiwoom(codes: list[str], end_date: date) -> pd.DataFrame:
    """OPT10081 서브프로세스 호출 — universe 종목만 (당일+과거 모두 가능한 차트형 TR)."""
    if not codes:
        return pd.DataFrame()
    KIWOOM_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    codes_file = KIWOOM_OUTPUT_DIR / f"ohlcv_codes_{end_date.strftime('%Y%m%d')}.txt"
    codes_file.write_text("\n".join(codes), encoding="utf-8")
    cmd = [KIWOOM_PYTHON, *KIWOOM_PYTHON_ARGS, str(KIWOOM_OHLCV_SCRIPT),
           "--codes-file", str(codes_file), "--end", str(end_date)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=KIWOOM_OHLCV_TIMEOUT_SEC)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        print(f"  [키움] OHLCV 서브프로세스 실행 실패: {e}")
        return pd.DataFrame()
    if proc.returncode != 0:
        print(f"  [키움] OHLCV 종료코드 {proc.returncode}: {proc.stderr.strip()[:500]}")
        return pd.DataFrame()
    out_path = KIWOOM_OUTPUT_DIR / f"kiwoom_ohlcv_{end_date.strftime('%Y%m%d')}.csv"
    if not out_path.exists():
        print(f"  [키움] OHLCV 출력 파일 없음: {out_path}")
        return pd.DataFrame()
    df = pd.read_csv(out_path, encoding="utf-8-sig", dtype={"code": str})
    if df.empty:
        return df
    print(f"  [키움] OHLCV 수집 성공: {len(df)}행 ({df['code'].nunique()}종목)")
    return df


def _load_code_market_map() -> dict:
    """code -> market 매핑. dim_stock(Postgres)은 최근 6개월치만 유지되어
    250거래일 universe 일부 종목이 누락됨(2026-06-17 실측: 1242종목 중 84,206행
    스킵) — 대신 fact_daily_stock parquet 전체(같은 lookback 범위)에서 직접
    최신 시장구분을 가져와 누락 없이 매핑."""
    return _load_code_info_df().set_index("code")["market"].to_dict()


def _load_code_name_map() -> dict:
    """code -> name 매핑. opt10081 응답엔 종목명이 없어 fact_daily_stock에서 보강.

    2026-06-17 발견: 이 매핑 없이 name 컬럼을 그냥 None으로 채워 upsert하면
    drop_duplicates(keep='last')에서 키움(새 행, name=None)이 기존 data.go.kr
    데이터(name=실값)를 통째로 덮어써 268일×318,247행(전체의 33.4%)의 종목명이
    None으로 손상되는 회귀 버그가 있었음 — merge_ohlcv_into_market_store()에서
    new_rows 작성 시 반드시 이 맵으로 먼저 채워야 함."""
    return _load_code_info_df().set_index("code")["name"].to_dict()


def _load_code_info_df() -> pd.DataFrame:
    today = date.today()
    months = sorted({(today - timedelta(days=30 * i)).strftime("%Y%m") for i in range(14)})
    frames = []
    for ym in months:
        p = PARQUET_DIR / "fact_daily_stock" / f"yearmonth={ym}" / "data.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p, columns=["date", "code", "name", "market"]))
    if not frames:
        return pd.DataFrame(columns=["date", "code", "name", "market"])
    df = pd.concat(frames, ignore_index=True).sort_values("date")
    return df.drop_duplicates("code", keep="last")


MARKETS_ALL = ["KOSPI", "KOSDAQ"]


def _purge_stale_cross_market_rows(ym: str, market: str, codes: set[str]) -> int:
    """정책: 종목 1개 = fact_daily_stock 최신 시장구분 기준 단일 폴더만 유지.
    같은 종목이 '다른' market 폴더에도 남아있으면 그쪽은 stale로 간주해 제거.

    2026-06-17 발견: 파라다이스/엘앤에프/비에이치 3종목이 KOSPI·KOSDAQ 양쪽
    폴더에 동시 존재(804행). data.go.kr 라이브 재조회로 현재는 KOSPI 단일
    분류임을 확인 — 거래소(KRX/NXT) 중복이 아니라, 과거 시장 이전상장 등으로
    생긴 stale 잔존 데이터로 판단됨(data.go.kr 응답엔 거래소구분 필드 자체가
    없어 KRX/NXT 기준 필터링은 적용 불가). 재발 방지를 위해 매 merge 시점에
    "방금 쓴 market과 다른 폴더"에 같은 종목코드가 있으면 제거."""
    other = [m for m in MARKETS_ALL if m != market]
    removed = 0
    for om in other:
        for p in MARKET_OHLCV_DIR.glob(f"yearmonth={ym}/{om}/data.parquet"):
            other_df = pd.read_parquet(p)
            mask = other_df["code"].isin(codes)
            if mask.any():
                removed += int(mask.sum())
                other_df[~mask].to_parquet(p, index=False, compression="snappy")
                print(f"  [정리] {ym}/{om}: {market}으로 이전상장된 종목 stale 행 {int(mask.sum())}건 제거")
    return removed


def merge_ohlcv_into_market_store(df: pd.DataFrame) -> dict:
    """키움 OHLCV df(date/code/open/high/low/close/volume/amount)를
    data/market/ohlcv/yearmonth=YYYYMM/{market}/data.parquet 저장소에 upsert.
    market 정보는 fact_daily_stock에서 join(opt10081 응답엔 종목명/시장구분이 없음)."""
    if df.empty:
        return {"status": "skipped", "reason": "empty input"}

    code_market = _load_code_market_map()
    df = df.copy()
    df["market"] = df["code"].map(code_market)
    n_unmapped = df["market"].isna().sum()
    if n_unmapped:
        print(f"  [경고] dim_stock에 없는 종목코드 {n_unmapped}행 스킵 (시장구분 매핑 불가)")
    df = df.dropna(subset=["market"])
    if df.empty:
        return {"status": "skipped", "reason": "no rows after market mapping"}

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["yearmonth"] = df["date"].astype(str).str.replace("-", "").str[:6]

    n_written = 0
    n_purged = 0
    for (ym, market), part in df.groupby(["yearmonth", "market"]):
        n_purged += _purge_stale_cross_market_rows(ym, market, set(part["code"].unique()))
        p = MARKET_OHLCV_DIR / f"yearmonth={ym}" / market / "data.parquet"
        if p.exists():
            existing = pd.read_parquet(p)
            existing["date"] = pd.to_datetime(existing["date"].astype(str)).dt.date
        else:
            existing = pd.DataFrame()
        new_rows = part.drop(columns=["yearmonth", "market"]).copy()
        new_rows["market"] = market
        # name은 fact_daily_stock에서 채움 (opt10081엔 종목명이 없음).
        # change/change_pct/shares/mktcap도 opt10081엔 없는 필드인데, 무작정 None으로
        # 채워 upsert하면 drop_duplicates(keep='last')에서 기존 data.go.kr 행의 실값을
        # 통째로 지워버리므로(2026-06-17 발견한 회귀 버그), 같은 (date,code)에 기존 값이
        # 있으면 그대로 이어받고, 없을 때만 None으로 둔다.
        code_name = _load_code_name_map()
        new_rows["name"] = new_rows["code"].map(code_name)
        carry_cols = ["change", "change_pct", "shares", "mktcap"]
        if not existing.empty and all(c in existing.columns for c in carry_cols):
            carry = existing[["date", "code"] + carry_cols].drop_duplicates(["date", "code"])
            new_rows = new_rows.merge(carry, on=["date", "code"], how="left")
        else:
            for col in carry_cols:
                new_rows[col] = None
        combined = pd.concat([existing, new_rows], ignore_index=True)
        combined = combined.drop_duplicates(subset=["date", "code"], keep="last")
        p.parent.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(p, index=False, compression="snappy")
        n_written += len(new_rows)
    return {"status": "done", "rows": n_written, "purged_stale_cross_market": n_purged}


def run_ohlcv_update(end_date: Optional[date] = None, prefer_kiwoom: bool = True) -> dict:
    """OHLCV 갱신: universe(거래대금 상위 1년 누적) → 키움, 나머지는 기존
    collect_ohlcv.py(data.go.kr, 전종목)가 평소처럼 익일 보완하므로 별도 처리 불필요
    — 이 함수는 universe 종목의 "당일 즉시 갱신"만 책임진다."""
    end_date = end_date or date.today()
    universe = compute_ohlcv_universe()
    print(f"OHLCV universe: {len(universe)}종목 (최근 {OHLCV_UNIVERSE_LOOKBACK_TRADING_DAYS}거래일 거래대금 상위 누적)")
    if not universe:
        return {"status": "skipped", "reason": "empty universe"}

    df = collect_ohlcv_kiwoom(universe, end_date) if prefer_kiwoom else pd.DataFrame()
    if df.empty:
        print("  [경고] 키움 OHLCV 수집 실패 — data.go.kr 익일 보완에 의존 (이번 배치는 스킵)")
        return {"status": "failed", "reason": "kiwoom collection empty"}

    result = merge_ohlcv_into_market_store(df)
    result["universe_size"] = len(universe)
    return result


# ── 테마 (정적, parquet에서 역변환) ───────────────────────────────────────────────

def load_theme_df() -> pd.DataFrame:
    df = pd.read_parquet(PARQUET_DIR / "dim_theme" / "data.parquet")
    return df.rename(columns={
        "name": "종목명", "code": "종목코드",
        "theme1": "테마(1차)", "theme2": "테마(2차)", "shares": "상장주식수",
    })


# ── 기존 월 파티션과 머지 (upsert) ────────────────────────────────────────────────

def _yearmonths(dates: list[date]) -> list[str]:
    return sorted({d.strftime("%Y%m") for d in dates})


def _load_existing_stock_months(yearmonths: list[str]) -> pd.DataFrame:
    frames = []
    for ym in yearmonths:
        p = PARQUET_DIR / "fact_daily_stock" / f"yearmonth={ym}" / "data.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out.rename(columns={
        "date": "날짜", "rank": "순위", "code": "종목코드", "name": "종목명",
        "market": "시장구분", "base_price": "시작일기준가", "close_price": "종료일종가",
        "change": "대비", "change_pct": "등락률",
        "volume_sum": "거래량_합계", "volume_avg": "거래량_일평균",
        "amount_sum": "거래대금_합계", "amount_avg": "거래대금_일평균",
    })[STOCK_COLS]


def _load_existing_kospi_months(yearmonths: list[str]) -> pd.DataFrame:
    frames = []
    for ym in yearmonths:
        p = PARQUET_DIR / "fact_kospi" / f"yearmonth={ym}" / "data.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out.rename(columns={
        "date": "Date", "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume", "change_rate": "Increase rate",
    })


def _load_existing_adr_months(yearmonths: list[str]) -> pd.DataFrame:
    frames = []
    for ym in yearmonths:
        p = PARQUET_DIR / "fact_adr" / f"yearmonth={ym}" / "data.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "is_verified" not in out.columns:
        out["is_verified"] = True  # 005_adr_verified_flag.sql 이전 파티션 호환
    return out.rename(columns={
        "date": "날짜", "index_name": "지수",
        "up_count": "상승종목수", "down_count": "하락종목수",
        "flat_count": "보합종목수", "adr": "하락 대비 상승비율",
    })


def _upsert(existing: pd.DataFrame, new: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    if existing.empty:
        return new
    if new.empty:
        return existing
    combined = pd.concat([existing, new], ignore_index=True)
    for c in key_cols:
        combined[c] = combined[c].astype(str)
    return combined.drop_duplicates(subset=key_cols, keep="last").reset_index(drop=True)


# ── incoming xlsx 빌드 ──────────────────────────────────────────────────────────

def build_incoming_workbook(stock_df: pd.DataFrame, kospi_df: pd.DataFrame,
                            adr_df: pd.DataFrame, theme_df: pd.DataFrame,
                            out_path: Path) -> None:
    from ssen.derived.calc_derived import add_derived_columns
    full_stock = add_derived_columns(stock_df, theme_df, kospi_df)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        full_stock.to_excel(writer, sheet_name="거래대금", index=False)
        kospi_df.to_excel(writer, sheet_name="코스피시세", index=False)
        adr_df.to_excel(writer, sheet_name="상승하락비율", index=False)
        theme_df.to_excel(writer, sheet_name="테마", index=False)


# ── ADR 검증 전 임시값(is_verified=false) 정리 ───────────────────────────────────

CONFIRMED_PATH = ROOT / "data" / "adr_verify" / "CONFIRMED.json"


def find_unverified_adr_dates() -> list[date]:
    """fact_adr 전체 파티션에서 is_verified=False로 남은 날짜 목록."""
    import json
    dates: set[date] = set()
    for p in sorted((PARQUET_DIR / "fact_adr").glob("yearmonth=*/data.parquet")):
        df = pd.read_parquet(p, columns=["date", "is_verified"]) if _has_column(p, "is_verified") else None
        if df is None:
            continue
        bad = df[df["is_verified"] == False]  # noqa: E712 (pandas bool 컬럼 명시 비교)
        dates.update(pd.to_datetime(bad["date"].astype(str)).dt.date.tolist())
    return sorted(dates)


def _has_column(parquet_path: Path, col: str) -> bool:
    import pyarrow.parquet as pq
    return col in pq.read_schema(parquet_path).names


def reconcile_adr() -> dict:
    """검증 완료(CONFIRMED.json) 후 is_verified=False로 남은 ADR을 정리.

    - 검증 결과가 '동일'이면 당시 캡처값도 결국 확정치와 같았다는 뜻 → 재수집 없이
      플래그만 True로 전환
    - '다름'이면 당시 캡처값이 부정확할 수 있음 → data.go.kr 확정치로 재수집해 덮어씀
      (data.go.kr은 1~2일 지연이라 보통 다음날 이후 해당 날짜가 공식 발표되어 있음).
      키움은 "당일 스냅샷"만 지원해 이미 지나간 날짜를 다른 시각으로 재조회할 수 없으므로
      대상이 아님(daily_update.py 본 운영과 동일한 역할 분리).
    """
    import json
    from ssen.etl.convert_excel_to_parquet import _write_partition, _load_manifest, _save_manifest, FACT_ADR_SCHEMA

    if not CONFIRMED_PATH.exists():
        return {"status": "skipped", "reason": "verification not confirmed yet (CONFIRMED.json 없음)"}
    verdict = json.load(open(CONFIRMED_PATH, encoding="utf-8"))

    bad_dates = find_unverified_adr_dates()
    if not bad_dates:
        return {"status": "skipped", "reason": "no unverified rows"}

    yms = _yearmonths(bad_dates)
    existing = _load_existing_adr_months(yms)

    if verdict["identical"]:
        mask = existing["날짜"].astype(str).isin([str(d) for d in bad_dates])
        existing.loc[mask, "is_verified"] = True
        merged = existing
        action = "flag_only (검증결과 동일 — 재수집 불필요)"
    else:
        new_adr = collect_adr_datagokr(min(bad_dates), max(bad_dates))
        merged = _upsert(existing, new_adr, ["날짜", "지수"])
        action = "recollected_from_datagokr (검증결과 차이 — data.go.kr 확정치로 교체)"

    manifest_path = PARQUET_DIR / "_manifest.json"
    manifest = _load_manifest(manifest_path)
    df_write = merged.rename(columns={
        "날짜": "date", "지수": "index_name",
        "상승종목수": "up_count", "하락종목수": "down_count",
        "보합종목수": "flat_count", "하락 대비 상승비율": "adr",
    })
    df_write["date"] = pd.to_datetime(df_write["date"]).dt.date
    for ym in yms:
        part = df_write[df_write["date"].astype(str).str.replace("-", "").str[:6] == ym].copy()
        if part.empty:
            continue
        meta = _write_partition(part, PARQUET_DIR / "fact_adr" / f"yearmonth={ym}", FACT_ADR_SCHEMA, ym)
        manifest["fact_adr"][ym] = meta
    _save_manifest(manifest, manifest_path)

    from ssen.db.load_parquet_to_postgres import load as pg_load
    pg_load(parquet_dir=PARQUET_DIR, months=yms, overlap_policy="rebuild")

    try:
        from ssen.api import cache as _cache
        _cache.invalidate_prefix("leaders")
        _cache.invalidate_prefix("trades")
    except Exception:
        pass

    return {"status": "done", "action": action, "dates": [str(d) for d in bad_dates], "yearmonths": yms}


# ── 메인 오케스트레이션 ───────────────────────────────────────────────────────────

def run(target_dates: Optional[list[date]] = None, prefer_kiwoom: bool = True,
        skip_e3: bool = False) -> dict:
    """target_dates 누락분(기본: 자동 산출)을 수집→머지→ingest."""
    target_dates = target_dates or missing_business_days()
    if not target_dates:
        print("누락된 영업일 없음 — 최신 상태")
        return {"status": "skipped", "reason": "no missing business days"}

    print(f"대상 영업일: {[str(d) for d in target_dates]}")

    # ── 1. 거래대금: 날짜별로 키움 → 실패 시 data.go.kr 폴백 ────────────────────
    stock_frames = []
    for d in target_dates:
        df = collect_stock_kiwoom(d) if prefer_kiwoom else None
        if df is None or df.empty:
            df = collect_stock_datagokr(d)
        if df.empty:
            print(f"  [경고] {d} 거래대금 수집 실패 (스킵, logs 기록)")
            continue
        stock_frames.append(df)
    if not stock_frames:
        return {"status": "failed", "reason": "no stock data collected"}
    new_stock = pd.concat(stock_frames, ignore_index=True)

    # ── 2. 코스피시세: 키움(당일+백필 모두) → 실패 시 data.go.kr 폴백 ────────────
    start, end = min(target_dates), max(target_dates)
    new_kospi = collect_kospi_kiwoom(start, end) if prefer_kiwoom else pd.DataFrame()
    if new_kospi.empty:
        new_kospi = collect_kospi_datagokr(start, end)

    # ── 3. ADR: 과거=data.go.kr 전용 / 오늘=키움 전용 (역할 분리, 폴백 아님) ─────
    today = date.today()
    past_dates = [d for d in target_dates if d != today]
    adr_frames = []
    if past_dates:
        adr_frames.append(collect_adr_datagokr(min(past_dates), max(past_dates)))
    if today in target_dates:
        today_adr = collect_adr_kiwoom(today) if prefer_kiwoom else pd.DataFrame()
        if today_adr.empty:
            print(f"  [경고] 오늘({today}) ADR 키움 수집 실패 — data.go.kr은 당일 데이터를 "
                  f"구조적으로 제공하지 않아 폴백 불가, 이번 배치에서 오늘 ADR 누락 (다음 실행 시 재시도)")
        else:
            adr_frames.append(today_adr)
    new_adr = pd.concat(adr_frames, ignore_index=True) if adr_frames else pd.DataFrame()

    # ── 4. 기존 월 파티션과 머지(upsert) ─────────────────────────────────────
    yms = _yearmonths(target_dates)
    merged_stock = _upsert(_load_existing_stock_months(yms), new_stock, ["날짜", "종목코드"])
    merged_kospi = _upsert(_load_existing_kospi_months(yms), new_kospi, ["Date"])
    merged_adr = _upsert(_load_existing_adr_months(yms), new_adr, ["날짜", "지수"])
    theme_df = load_theme_df()

    # ── 5. OHLCV: universe(거래대금 상위 1년 누적) 키움 갱신 + data.go.kr 전종목 백스톱 ──
    # dataset_version을 갱신하는 6번(ingest)보다 반드시 먼저 실행한다. 역순으로 두면
    # dataset_version이 먼저 바뀌고 market_ohlcv는 한참 뒤(키움 OHLCV는 universe
    # 전체 호출에 수분~수십분 소요)에야 채워지는데, 그 사이 창에서 RS 계산(market_ohlcv
    # 기준)이 새 dataset_version으로 캐시되면 그날 종가가 빠진 RS가 다음날까지
    # 영구 캐시되는 회귀가 있었음(2026-06-19 발견: leader-events/dominant-top-stocks의
    # 당일 RS점수가 전부 결측으로 캐시됨). compute_ohlcv_universe()가 직전일까지의
    # fact_daily_stock으로 universe를 정하는 부작용은 있으나(당일 신규 상위종목 1일
    # 지연 — 다음날 자연 보완), RS 캐시 영구 오염보다 훨씬 가벼운 trade-off.
    print("\nOHLCV 갱신...")
    ohlcv_result = run_ohlcv_update(end_date=max(target_dates), prefer_kiwoom=prefer_kiwoom)
    print(f"  {ohlcv_result}")
    run_ohlcv_datagokr_backstop(start=min(target_dates), end=max(target_dates))

    # ── 6. incoming xlsx 빌드 + ingest (E1→E2→E3→캐시무효화→archive) ───────────
    incoming_path = INCOMING_DIR / f"daily_update_{datetime.now().strftime('%Y%m%dT%H%M%S')}.xlsx"
    build_incoming_workbook(merged_stock, merged_kospi, merged_adr, theme_df, incoming_path)
    print(f"incoming workbook 생성: {incoming_path}")

    from ssen.pipeline.update_all import run as pipeline_run
    result = pipeline_run(
        incoming_dir=INCOMING_DIR, parquet_dir=PARQUET_DIR,
        overlap="rebuild", skip_e3=skip_e3,
    )
    result["target_dates"] = [str(d) for d in target_dates]
    result["ohlcv"] = ohlcv_result

    return result


def run_ohlcv_datagokr_backstop(start: date, end: date) -> dict:
    """기존 collect_ohlcv.py(data.go.kr, 전종목)로 같은 기간을 한 번 더 수집해
    universe 밖 종목(키움 미수집분)을 보완. data.go.kr 1~2일 지연 특성상 가장 최근
    날짜는 비어있을 수 있음 — 정상 동작이며 다음 실행에서 자연히 채워짐."""
    try:
        from ssen.market.collect_ohlcv import collect as ohlcv_collect, DEFAULT_KEY
        ohlcv_collect(
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
            markets=["KOSPI", "KOSDAQ"], service_key=DEFAULT_KEY, resume=False,
        )
        return {"status": "done"}
    except Exception as e:
        print(f"  [경고] OHLCV data.go.kr 백스톱 실패: {e}")
        return {"status": "failed", "reason": str(e)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="E9: 일일 수집/백필 파이프라인")
    parser.add_argument("--start", help="백필 시작일 YYYY-MM-DD (생략 시 자동 산출)")
    parser.add_argument("--end", help="백필 종료일 YYYY-MM-DD (생략 시 오늘)")
    parser.add_argument("--no-kiwoom", action="store_true", help="키움 경로 생략, data.go.kr만 사용")
    parser.add_argument("--skip-e3", action="store_true")
    parser.add_argument("--reconcile-adr", action="store_true",
                        help="ADR 확정시점 검증(CONFIRMED.json) 완료 후 is_verified=False 행 정리")
    parser.add_argument("--ohlcv-only", action="store_true",
                        help="OHLCV(universe 키움 + data.go.kr 백스톱)만 실행, 거래대금/코스피시세/ADR 생략")
    args = parser.parse_args()

    if args.reconcile_adr:
        print(reconcile_adr())
        raise SystemExit(0)

    if args.ohlcv_only:
        end = date.fromisoformat(args.end) if args.end else date.today()
        start = date.fromisoformat(args.start) if args.start else end
        print(run_ohlcv_update(end_date=end, prefer_kiwoom=not args.no_kiwoom))
        print(run_ohlcv_datagokr_backstop(start, end))
        raise SystemExit(0)

    dates = None
    if args.start:
        end = date.fromisoformat(args.end) if args.end else date.today()
        dates = [d.date() for d in pd.bdate_range(date.fromisoformat(args.start), end)]

    res = run(target_dates=dates, prefer_kiwoom=not args.no_kiwoom, skip_e3=args.skip_e3)
    print(res)

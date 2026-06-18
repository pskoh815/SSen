"""
OHLCV 전체 시장 수집 (data.go.kr API)
Bull_Bear.py 패턴 기반 — 월별 구간 분할 + 페이지네이션

Usage:
    python -m ssen.market.collect_ohlcv [OPTIONS]

Options:
    --start-date  YYYYMMDD  (기본: 20200102)
    --end-date    YYYYMMDD  (기본: 오늘)
    --market      KOSPI|KOSDAQ|ALL (기본: ALL)
    --resume      이미 수집된 월은 건너뜀
    --service-key 서비스키 (기본: 환경변수 DATA_GO_KR_KEY)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[3]
MARKET_DIR = ROOT / "data" / "market" / "ohlcv"
PROGRESS_FILE = MARKET_DIR / "_progress.json"

API_URL = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
DEFAULT_KEY = "7f2e200b8c8d0a0f01d39084322743b0b60c9eec801a70494e619bb89a57f410"

# API 응답 → 표준 컬럼 매핑
FIELD_MAP = {
    "basDt":       "date",
    "srtnCd":      "code",
    "itmsNm":      "name",
    "mrktCtg":     "market",
    "clpr":        "close",
    "mkp":         "open",
    "hipr":        "high",
    "lopr":        "low",
    "vs":          "change",
    "fltRt":       "change_pct",
    "trqu":        "volume",
    "trPrc":       "amount",
    "lstgStCnt":   "shares",
    "mrktTotAmt":  "mktcap",
}
NUMERIC_COLS = ["close","open","high","low","change","change_pct","volume","amount","shares","mktcap"]


# ── 날짜 유틸 ─────────────────────────────────────────────────────────────────

def monthly_ranges(start: str, end: str) -> list[tuple[str, str]]:
    """'20200102','20261231' → [('20200102','20200131'), ...] 월별 분할"""
    s = datetime.strptime(start, "%Y%m%d")
    e = datetime.strptime(end,   "%Y%m%d")
    ranges, cur = [], s
    while cur <= e:
        if cur.month == 12:
            month_end = datetime(cur.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = datetime(cur.year, cur.month + 1, 1) - timedelta(days=1)
        ranges.append((cur.strftime("%Y%m%d"), min(month_end, e).strftime("%Y%m%d")))
        cur = (datetime(cur.year, cur.month + 1, 1) if cur.month < 12
               else datetime(cur.year + 1, 1, 1))
    return ranges


def ym_key(begin_dt: str) -> str:
    return begin_dt[:6]  # '202001'


# ── 진행 상황 저장 ────────────────────────────────────────────────────────────

def load_progress() -> set[str]:
    """완료된 yearmonth 집합 로드."""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("done", []))
    return set()


def save_progress(done: set[str]) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done)}, f, ensure_ascii=False)


# ── API 수집 ──────────────────────────────────────────────────────────────────

def fetch_month(begin_dt: str, end_dt: str, market: str, service_key: str,
                max_rows: int = 3000) -> list[dict]:
    """한 월, 한 시장의 전체 데이터 페이지네이션 수집."""
    all_items = []
    page_no   = 1

    while True:
        params = {
            "serviceKey": service_key,
            "numOfRows":  max_rows,
            "pageNo":     page_no,
            "resultType": "json",
            "mrktCls":    market,
            "beginBasDt": begin_dt,
            "endBasDt":   end_dt,
        }

        for attempt in range(3):
            try:
                r = requests.get(API_URL, params=params, timeout=30)
                if r.status_code != 200:
                    print(f"    HTTP {r.status_code}, 재시도 {attempt+1}/3")
                    time.sleep(2); continue

                body  = r.json().get("response", {}).get("body", {})
                items = body.get("items", {})
                if not items:
                    return all_items

                item_list = items.get("item", [])
                if isinstance(item_list, dict):
                    item_list = [item_list]
                if not item_list:
                    return all_items

                all_items.extend(item_list)
                total = int(body.get("totalCount", 0))
                print(f"    p{page_no:>2} | +{len(item_list):>4}건 | 누적 {len(all_items):>5}/{total}건")

                if page_no * max_rows >= total:
                    return all_items
                page_no += 1
                time.sleep(0.3)
                break

            except Exception as exc:
                print(f"    예외: {exc}, 재시도 {attempt+1}/3")
                time.sleep(2)
        else:
            print("    최대 재시도 초과, 이 구간 종료")
            return all_items

    return all_items


# ── 전처리 & 저장 ─────────────────────────────────────────────────────────────

def to_parquet(items: list[dict], ym: str, market: str) -> Path:
    df = pd.DataFrame(items).rename(columns=FIELD_MAP)
    # 필요한 컬럼만 선택 (없는 컬럼은 None)
    for col in FIELD_MAP.values():
        if col not in df.columns:
            df[col] = None
    df = df[list(FIELD_MAP.values())].copy()

    # 타입 변환
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.date
    df["code"] = df["code"].astype(str).str.zfill(6)
    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    out_dir  = MARKET_DIR / f"yearmonth={ym}" / market
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "data.parquet"
    df.to_parquet(out_file, index=False, compression="snappy")
    return out_file


# ── 메인 ─────────────────────────────────────────────────────────────────────

def collect(start_date: str, end_date: str, markets: list[str],
            service_key: str, resume: bool = True) -> None:
    done = load_progress() if resume else set()
    ranges = monthly_ranges(start_date, end_date)

    total_months = len(ranges) * len(markets)
    processed = 0

    print(f"\n{'='*60}")
    print(f"OHLCV 수집: {start_date} ~ {end_date}")
    print(f"시장: {markets} | 월별 구간: {len(ranges)}개 | 총 {total_months}회")
    print(f"저장 위치: {MARKET_DIR}")
    print(f"{'='*60}\n")

    for market in markets:
        for begin_dt, end_dt in ranges:
            ym  = ym_key(begin_dt)
            key = f"{ym}_{market}"

            if resume and key in done:
                print(f"  skip {ym} [{market}] (이미 완료)")
                continue

            processed += 1
            remaining = total_months - len(done) - processed + 1
            print(f"\n[{processed}/{total_months - len(done)}] "
                  f"{begin_dt}~{end_dt} [{market}] 수집...")

            items = fetch_month(begin_dt, end_dt, market, service_key)

            if items:
                out = to_parquet(items, ym, market)
                sz  = out.stat().st_size / 1024
                print(f"  저장: {out.name} ({len(items):,}행, {sz:.0f}KB)")
                done.add(key)
                save_progress(done)
            else:
                print(f"  데이터 없음")

            time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"수집 완료: {len(done)}개 월-시장 파티션")
    print(f"{'='*60}")


def main():
    today = datetime.today().strftime("%Y%m%d")

    parser = argparse.ArgumentParser(description="OHLCV 수집 (data.go.kr)")
    parser.add_argument("--start-date",  default="20200102")
    parser.add_argument("--end-date",    default=today)
    parser.add_argument("--market",      default="ALL",
                        choices=["KOSPI","KOSDAQ","ALL"])
    parser.add_argument("--no-resume",   action="store_true",
                        help="이미 수집된 월도 재수집")
    parser.add_argument("--service-key", default=None)
    args = parser.parse_args()

    key = (args.service_key
           or os.environ.get("DATA_GO_KR_KEY", DEFAULT_KEY))
    key = unquote(key)

    markets = (["KOSPI","KOSDAQ"] if args.market == "ALL"
               else [args.market])

    collect(
        start_date  = args.start_date,
        end_date    = args.end_date,
        markets     = markets,
        service_key = key,
        resume      = not args.no_resume,
    )


if __name__ == "__main__":
    main()

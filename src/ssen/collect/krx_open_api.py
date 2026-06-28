# -*- coding: utf-8 -*-
"""KRX OPEN API(data-dbg.krx.co.kr) — 유가증권/코스닥 일별매매정보 수집.

목적: 2015~2019년 키움 기반 OHLCV 백필(universe=현재 거래대금 상위 250거래일 누적)이
가진 서바이버십 편향(그 당시 거래대금 상위였으나 이후 상장폐지/합병된 종목이 universe
정의상 처음부터 빠짐)을 보완한다. KRX API는 그 날짜에 "실제 상장돼 있던" 모든 종목을
그대로 반환하므로, 당시 진짜 거래대금 상위 50종목을 day-by-day로 재구성할 수 있다.

엔드포인트(둘 다 스키마 동일):
  유가증권(코스피): https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd
  코스닥:          https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd
인증: AUTH_KEY 헤더(.env의 OPENKRX_KEY). 가격은 원시가(수정주가 미반영) — 기존
fact_daily_stock/market_ohlcv와 동일 컨벤션이라 직접 비교 가능(2026-06-23 교차검증 완료:
005930 2026-06-19 종가/거래량/거래대금/시가총액 전부 일치).
"""
import argparse
import json
import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis/sto"
ENDPOINTS = {"KOSPI": f"{BASE_URL}/stk_bydd_trd", "KOSDAQ": f"{BASE_URL}/ksq_bydd_trd"}

ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / "data" / "incoming" / "krx_open_api" / "raw_cache"
REQUEST_SLEEP_SEC = 0.15


def load_api_key() -> str:
    key = os.getenv("OPENKRX_KEY")
    if not key:
        raise RuntimeError(".env에 OPENKRX_KEY가 설정되어 있지 않습니다")
    return key


def fetch_day(api_key: str, market: str, bas_dd: str) -> list[dict]:
    """market='KOSPI'|'KOSDAQ', bas_dd='YYYYMMDD'. 로컬 캐시 우선(재실행 시 API 재호출 없음)."""
    cache_path = CACHE_DIR / market / f"{bas_dd}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    resp = requests.get(ENDPOINTS[market], headers={"AUTH_KEY": api_key},
                         params={"basDd": bas_dd}, timeout=20)
    resp.raise_for_status()
    rows = resp.json().get("OutBlock_1", [])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    time.sleep(REQUEST_SLEEP_SEC)
    return rows


def collect_range(api_key: str, trading_days: list[str], markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")) -> dict:
    """trading_days(YYYYMMDD 문자열 리스트) 전체에 대해 두 시장 모두 수집.
    반환: {market: {basDd: [row, ...]}}"""
    result = {m: {} for m in markets}
    total = len(trading_days) * len(markets)
    done = 0
    for bas_dd in trading_days:
        for market in markets:
            done += 1
            try:
                rows = fetch_day(api_key, market, bas_dd)
            except requests.RequestException as e:
                print(f"  [경고] {market} {bas_dd} 조회 실패: {e}")
                rows = []
            result[market][bas_dd] = rows
            if done % 200 == 0:
                print(f"  진행 {done}/{total}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--trading-days-file", required=True,
                        help="거래일 목록 파일 (1행 1일, YYYY-MM-DD)")
    args = parser.parse_args()

    api_key = load_api_key()
    days = [d.strip().replace("-", "") for d in
            Path(args.trading_days_file).read_text(encoding="utf-8").splitlines() if d.strip()]
    print(f"대상 거래일수: {len(days)}")
    collect_range(api_key, days)
    print("[완료] 캐시 저장 위치:", CACHE_DIR)


if __name__ == "__main__":
    main()

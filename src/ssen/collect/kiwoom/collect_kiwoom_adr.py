# -*- coding: utf-8 -*-
"""키움 OpenAPI+ 상승하락비율(ADR) 당일 수집 (OPT20009 업종현재가일별요청).

반드시 32비트 Python으로 실행 (py -3.9-32 collect_kiwoom_adr.py [--date YYYY-MM-DD]).
기존 지수_상승종목수.py(data.go.kr)와 동일한 출력 스키마(날짜/지수/상승종목수/하락종목수/
보합종목수/하락 대비 상승비율)를 생성한다.

검증된 사실 (2026-06-17 실측):
  - OPT20009(코스피 업종코드=001, 시장구분=0 / 코스닥 업종코드=101, 시장구분=1)의
    싱글데이터에 상승/보합/하락/상한/하한/상장종목수가 모두 포함됨
    (예: 코스피 상승349 보합42 하락526, 코스닥 상승957 보합96 하락681)
  - 단, 이 TR은 "오늘 현재" 스냅샷만 제공 — 멀티데이터(일별 과거치)에는
    일자/현재가/등락률/거래량만 있고 상승·하락·보합 종목수는 없음.
    키움 TR 223개 전체 검토 결과 과거 날짜별 ADR을 주는 TR은 존재하지 않음
    → 과거 백필은 반드시 data.go.kr(지수_상승종목수.py) 사용, 이 스크립트는 당일 전용
  - 확정 시점(15:35 vs 16:30 동일 여부)은 verify_adr_timing.py로 별도 검증 중
    (docs/kiwoom_collection_spec.md "ADR 확정 시점 검증" 절 참조)
  - 검증 완료 전까지는 모든 행에 is_verified=False를 붙여 fact_adr에 "검증 전 임시값"임을
    표시한다 (verify_adr_timing.py compare가 CONFIRMED.json을 쓰기 전까지)
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
from pykiwoom.kiwoom import Kiwoom

from _watchdog import Watchdog  # 4개 키움 수집 스크립트 공용 (같은 디렉터리)

OUTPUT_DIR = Path(r"C:\MyClaude\ssen-dashboard\data\incoming\kiwoom")
CONFIRMED_PATH = Path(r"C:\MyClaude\ssen-dashboard\data\adr_verify\CONFIRMED.json")
MARKETS = [("001", "0", "KOSPI"), ("101", "1", "KOSDAQ")]
WATCHDOG_TIMEOUT_SEC = 60  # 시장 2개 × 1회 호출이라 60초면 충분 (2026-06-18 통일 적용)


def is_timing_confirmed() -> bool:
    """verify_adr_timing.py compare가 검증을 완료하고 CONFIRMED.json을 남겼는지 여부."""
    return CONFIRMED_PATH.exists()


def fetch_adr_today(kiwoom: Kiwoom, target_date: date, watchdog: Optional[Watchdog] = None) -> pd.DataFrame:
    verified = is_timing_confirmed()
    rows = []
    for index_code, market_gubun, market_name in MARKETS:
        df = kiwoom.block_request(
            "opt20009",
            시장구분=market_gubun,
            업종코드=index_code,
            output="업종현재가일별",
            next=0,
        )
        if watchdog:
            watchdog.reset()
        if df is None or df.empty:
            print(f"[경고] {market_name} 조회 결과 없음")
            continue
        r = df.iloc[0]
        up, flat, down = int(r["상승"]), int(r["보합"]), int(r["하락"])
        ratio = round(up / down * 100, 1) if down else 0.0
        rows.append({
            "날짜": target_date.strftime("%Y-%m-%d"),
            "지수": market_name,
            "상승종목수": up,
            "하락종목수": down,
            "보합종목수": flat,
            "하락 대비 상승비율": ratio,
            "is_verified": verified,
        })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="대상일 YYYY-MM-DD (생략 시 오늘, 키움은 당일만 지원)")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    if target_date != date.today():
        print(f"[오류] 키움 ADR(OPT20009)은 당일 스냅샷만 제공합니다 (요청: {target_date}, 오늘: {date.today()})")
        print("       과거 날짜는 ssen.collect.지수_상승종목수 (data.go.kr) 경로를 사용하세요.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    watchdog = Watchdog(WATCHDOG_TIMEOUT_SEC)
    watchdog.reset()  # CommConnect 자체가 멈추는 경우(중복 로그인 팝업 등)도 대비

    kiwoom = Kiwoom()
    kiwoom.CommConnect(block=True)
    watchdog.reset()
    if kiwoom.GetConnectState() != 1:
        print("[오류] 키움 OpenAPI 로그인 실패")
        sys.exit(1)
    print(f"[로그인 성공] 계좌: {kiwoom.GetLoginInfo('ACCNO')}")

    result = fetch_adr_today(kiwoom, target_date, watchdog)
    watchdog.stop()
    if result.empty:
        print("[오류] 수집 결과가 비어있음")
        sys.exit(1)

    out_path = OUTPUT_DIR / f"kiwoom_adr_{target_date.strftime('%Y%m%d')}.csv"
    result.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"[완료] {len(result)}행 저장 → {out_path}")
    print(result)


if __name__ == "__main__":
    main()

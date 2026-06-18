# -*- coding: utf-8 -*-
"""키움 OpenAPI+ 코스피 종합지수 일봉 수집 (OPT20006 업종일봉조회요청).

반드시 32비트 Python으로 실행 (py -3.9-32 collect_kiwoom_kospi.py [--start ..] [--end ..]).
기존 코스피지수_시세.py(data.go.kr)와 동일한 출력 스키마(Date/Open/High/Low/Close/Volume/
Increase rate)를 생성해 ingest 파이프라인을 그대로 재사용한다.

검증된 사실 (2026-06-17 실측):
  - OPT20006(업종일봉조회요청, 업종코드=001) 은 차트형 TR이라 과거 임의 날짜(기준일자) 백필 가능
    (기준일자=20260101 호출 시 600행 반환 확인) — opt10032(거래대금)와 달리 "당일만" 제약이 없음
  - 가격 단위: 지수값×100 정수, 부호 없음 (예: 872660 → 8726.60).
    OPT20009(업종현재가일별요청)의 같은 날짜 값(+8726.60)과 정확히 일치 — 교차검증 완료
  - 출력에 등락률/전일종가가 비어있어 직접 계산 필요 → 배치 내 연속일 종가로 pct_change 산출
    (요청 시 항상 충분한 과거분(수백 행)이 함께 오므로 윈도 시작일에도 직전일 종가 확보됨)
"""
import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from pykiwoom.kiwoom import Kiwoom

OUTPUT_DIR = Path(r"C:\MyClaude\ssen-dashboard\data\incoming\kiwoom")
INDEX_CODE = "001"  # 코스피 종합


def fetch_kospi_daily(kiwoom: Kiwoom, end_date: date) -> pd.DataFrame:
    """OPT20006으로 end_date 기준 과거 일봉 일괄 조회 → Date/Open/High/Low/Close/Volume/Increase rate."""
    df = kiwoom.block_request(
        "opt20006",
        업종코드=INDEX_CODE,
        기준일자=end_date.strftime("%Y%m%d"),
        output="업종일봉조회",
        next=0,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[df["일자"].astype(str).str.len() == 8].copy()
    if df.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["Date"] = pd.to_datetime(df["일자"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    for col, raw in [("Open", "시가"), ("High", "고가"), ("Low", "저가"), ("Close", "현재가")]:
        out[col] = df[raw].astype(float) / 100.0
    # 거래량 단위 변환: 천주 → 주 (실측 검증: data.go.kr 06-16 Volume=586,336,867
    # vs 키움 거래량=586337, 비율=999.9986 ≈ 1000 — 키움 OPT20006 업종거래량은 천주 단위)
    out["Volume"] = df["거래량"].astype(float) * 1000.0
    out = out.sort_values("Date").drop_duplicates("Date").reset_index(drop=True)
    out["Increase rate"] = (out["Close"].pct_change() * 100).round(2)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="필터 시작일 YYYY-MM-DD (생략 시 end-7일)")
    parser.add_argument("--end", help="기준일자 YYYY-MM-DD (생략 시 오늘)")
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    kiwoom = Kiwoom()
    kiwoom.CommConnect(block=True)
    if kiwoom.GetConnectState() != 1:
        print("[오류] 키움 OpenAPI 로그인 실패")
        sys.exit(1)
    print(f"[로그인 성공] 계좌: {kiwoom.GetLoginInfo('ACCNO')}")

    full = fetch_kospi_daily(kiwoom, end)
    if full.empty:
        print("[오류] 수집 결과가 비어있음")
        sys.exit(1)

    result = full[(full["Date"] >= str(start)) & (full["Date"] <= str(end))].reset_index(drop=True)
    if result.empty:
        print(f"[경고] {start}~{end} 범위에 해당하는 행 없음 (휴장일 가능)")
        sys.exit(1)

    out_path = OUTPUT_DIR / f"kiwoom_kospi_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.csv"
    result.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"[완료] {len(result)}행 저장 → {out_path}")
    print(result)


if __name__ == "__main__":
    main()

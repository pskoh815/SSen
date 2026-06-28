# -*- coding: utf-8 -*-
"""키움 OpenAPI+ 거래대금 상위 수집기.

반드시 32비트 Python으로 실행 (py -3.9-32 collect_kiwoom_money.py).
기존 SSen_Money.py(data.go.kr)와 동일한 출력 스키마(A~M열)를 생성해
ingest 파이프라인을 그대로 재사용한다.

검증된 사실 (2026-06-17 실측):
  - opt10032(거래대금상위요청) 거래대금 단위 = 백만원 (×1,000,000 = 원)
    근거: 삼성전자 6,085,609 × 1,000,000 ≈ 6.09조원 (실제 거래대금과 일치)
  - 가격/대비 컬럼에 +/- 부호 문자가 포함됨 → 숫자 변환 시 그대로 float() 가능
  - 전일대비기호: 1=상한 2=상승 3=보합 4=하한 5=하락 (등락률 부호 검증용)
  - 100종목 조회 시 ETF/ETN이 약 41% 혼입 (KODEX/TIGER/SOL/ARIRANG 등) → 반드시 제외
  - 장 마감(15:30) 후 15:40~16:00 사이 당일 데이터 조회 가능 (data.go.kr 익일 09:00+ 대비
    약 17시간 단축)
"""
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from pykiwoom.kiwoom import Kiwoom

from _watchdog import Watchdog  # 4개 키움 수집 스크립트 공용 (같은 디렉터리)

# ── 설정 ──────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(r"C:\MyClaude\ssen-dashboard\data\incoming\kiwoom")
TOP_N = 300                     # 시장별 수집 종목 수 (data.go.kr MostActiveStocks와 맞춤)
MARKETS = {"001": "KOSPI", "101": "KOSDAQ"}
# 이 시간 동안 진행 없으면 행(hang)으로 간주, 강제 종료 (2026-06-17 중복로그인 행 실측,
# 2026-06-18 daily_collect 인코딩 크래시 연쇄실패 계기로 money/kospi/adr에도 통일 적용).
# 이 스크립트는 시장 2개 × 1회 호출이라 60초면 충분한 여유.
WATCHDOG_TIMEOUT_SEC = 60

# ETF/ETN 브랜드 키워드 (종목명 기반 1차 필터, 운용사 신규 브랜드 추가 시 갱신 필요)
ETF_BRAND_PATTERN = re.compile(
    r"KODEX|TIGER|ARIRANG|KBSTAR|HANARO|SOL|RISE|ACE|KOSEF|TIMEFOLIO|PLUS|마이다스|VITA"
)
# ETF/ETN 종목코드 패턴: 단축코드가 6자리 숫자 외 영숫자 혼합(예: 0193T0)인 경우가 많음
ETF_CODE_PATTERN = re.compile(r"^\d{4}[A-Z]\d$")


def is_etf_like(code: str, name: str) -> bool:
    """ETF/ETN 추정 여부. 코드 패턴 또는 브랜드명 매칭 시 True."""
    if ETF_CODE_PATTERN.match(str(code)):
        return True
    if ETF_BRAND_PATTERN.search(str(name)):
        return True
    return False


def parse_signed_number(s) -> float:
    """'+346500', '-16000' 같은 부호 포함 문자열을 숫자로 변환 (대비/등락률처럼
    부호 자체가 의미있는 컬럼에만 사용 — 가격 컬럼에는 parse_price 사용)."""
    if pd.isna(s) or s == "":
        return 0.0
    return float(str(s).replace(",", ""))


def parse_price(s) -> float:
    """'현재가' 등 가격 컬럼 전용 파서. 키움은 가격 필드에도 전일대비 방향(상승/하락)을
    나타내는 +/- 기호를 붙이는데, 가격 자체는 절대 음수가 될 수 없으므로 부호를 버리고
    절댓값만 사용한다 (2026-06-17 발견: 현대로템 064350이 -219000원으로 저장돼
    테마 누적수익률이 -212%까지 떨어지는 버그의 원인이었음 — 등락률(-3.31%)은 부호가
    legit하지만 종료일종가는 음수가 되면 안 됨)."""
    return abs(parse_signed_number(s))


def collect_market(kiwoom: Kiwoom, market_code: str, market_name: str) -> pd.DataFrame:
    """시장 1개의 거래대금 상위를 조회해 SSen_Money.py 출력 스키마로 변환."""
    df = kiwoom.block_request(
        "opt10032",
        시장구분=market_code,
        관리종목포함="1",
        거래소구분="1",
        output="거래대금상위",
        next=0,
    )
    if df is None or df.empty:
        print(f"[경고] {market_name} 조회 결과 없음")
        return pd.DataFrame()

    df = df.head(TOP_N).copy()

    # ETF/ETN 제외
    before = len(df)
    df["_is_etf"] = df.apply(lambda r: is_etf_like(r["종목코드"], r["종목명"]), axis=1)
    excluded = df[df["_is_etf"]]
    df = df[~df["_is_etf"]].drop(columns="_is_etf").reset_index(drop=True)
    # reset_index 필수: 필터링 후 df의 인덱스가 비연속이면, 아래 out["순위"]=range(...)가
    # 만드는 새 0..N-1 인덱스와 정렬(align)되면서 행이 NaN으로 깨짐 (실측 발견된 버그)
    print(f"  {market_name}: {before}종목 → ETF/ETN {len(excluded)}개 제외 → {len(df)}종목")

    out = pd.DataFrame()
    out["순위"] = range(1, len(df) + 1)               # ETF 제외 후 순위 재계산 (먼저 대입해 행 길이 확정)
    out["날짜"] = datetime.now().strftime("%Y-%m-%d")  # 길이 확정 후 대입해야 전체 행에 스칼라가 브로드캐스트됨
    out["종목코드"] = df["종목코드"].astype(str)
    out["종목명"] = df["종목명"]
    out["시장구분"] = market_name
    # 시작일기준가: opt10032에는 없음(전일종가만 추정 가능) → 종료일종가-전일대비로 역산
    cur = df["현재가"].apply(parse_price)        # 가격 컬럼 — 부호는 등락방향일 뿐, 항상 양수
    diff = df["전일대비"].apply(parse_signed_number)  # 대비는 부호가 legit (하락 시 음수)
    out["시작일기준가"] = (cur - diff).round(0)
    out["종료일종가"] = cur
    out["대비"] = diff
    out["등락률"] = df["등락률"].apply(parse_signed_number)
    out["거래량_합계"] = df["현재거래량"].astype(float)
    out["거래량_일평균"] = out["거래량_합계"]            # 단일 거래일 수집이므로 합계=평균
    # 거래대금 단위 변환: 백만원 → 원 (실측 검증 완료, 위 docstring 참조)
    out["거래대금_합계"] = df["거래대금"].astype(float) * 1_000_000
    out["거래대금_일평균"] = out["거래대금_합계"]

    return out


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    watchdog = Watchdog(WATCHDOG_TIMEOUT_SEC)
    watchdog.reset()  # CommConnect 자체가 멈추는 경우(중복 로그인 팝업 등)도 대비

    kiwoom = Kiwoom()
    kiwoom.CommConnect(block=True)
    watchdog.reset()
    if kiwoom.GetConnectState() != 1:
        print("[오류] 키움 OpenAPI 로그인 실패 — AUTO 로그인 설정을 확인하세요")
        sys.exit(1)
    print(f"[로그인 성공] 계좌: {kiwoom.GetLoginInfo('ACCNO')}")

    frames = []
    for code, name in MARKETS.items():
        frames.append(collect_market(kiwoom, code, name))
        watchdog.reset()
        time.sleep(1)  # TR 호출 제한(초당 5회) 회피용 여유

    watchdog.stop()
    result = pd.concat(frames, ignore_index=True)
    if result.empty:
        print("[오류] 수집 결과가 비어있음 — 전체 중단 없이 종료 (CLAUDE.md 에러 정책)")
        sys.exit(1)

    today = datetime.now().strftime("%Y%m%d")
    out_path = OUTPUT_DIR / f"kiwoom_money_{today}.csv"
    result.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n[완료] {len(result)}행 저장 → {out_path}")
    print(result.groupby("시장구분").size())


if __name__ == "__main__":
    main()

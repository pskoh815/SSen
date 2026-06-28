# -*- coding: utf-8 -*-
"""OpenDART(전자공시시스템) 분기/사업보고서 재무 raw 데이터 수집.

fnlttSinglAcntAll(단일회사 전체 재무제표) 사용 — fnlttSinglAcnt(요약 API)는 기업이
직접 입력한 비표준 항목이라 누락이 많음. *All은 제출된 XBRL 표준계정 원장 그대로라
커버리지가 훨씬 높다 (2026-06-21 설계 합의).

수집 대상: 매출액/영업이익의 "분기·반기·3분기·사업보고서별 누적치"(thstrm_amount)를
account_id 매칭으로 추출해 raw 그대로 저장한다. K-IFRS 보고서는 회계연도 내 누적
기준이라(반기보고서=H1 누적, 3분기보고서=9개월 누적), 단독 분기(Q2/Q3/Q4) 수치가
필요하면 이후 분석 단계에서 차감 방식으로 별도 도출할 것 — 이 스크립트는 raw 수집만
담당(YoY/QoQ 등 지표 계산은 analysis 레이어 책임).

룩어헤드 방지의 핵심 컬럼은 rcept_dt(공시 접수일)이다 — 분기말이 아니라 이 날짜를
기준으로 가격 데이터와 매칭해야 한다(분기보고서는 분기말+45일, 사업보고서는 +90일
이내 공시).
"""
import argparse
import json
import os
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://opendart.fss.or.kr/api"
CORP_CODE_URL = f"{BASE_URL}/corpCode.xml"
FINSTATE_URL = f"{BASE_URL}/fnlttSinglAcntAll.json"

ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / "data" / "incoming" / "opendart"
CORP_CODE_CACHE = CACHE_DIR / "corp_code_map.parquet"
RAW_CACHE_DIR = CACHE_DIR / "raw_cache"  # (corp_code, year, reprt_code, fs_div) 단위 응답 캐시
OUTPUT_PATH = ROOT / "data" / "parquet" / "fact_financials" / "data.parquet"
MISSING_LOG = ROOT / "logs" / "dart_missing.csv"

# 사업보고서(연간)=11011, 1분기=11013, 반기=11012, 3분기=11014
REPRT_CODES = ["11013", "11012", "11014", "11011"]
REQUEST_SLEEP_SEC = 0.3  # 분당 트래픽 제한 회피용 호출 간 여유

# XBRL 계정ID가 회사마다 갈리는 문제 대응 — 후보를 우선순위로 매칭
REVENUE_CANDIDATES = [
    "ifrs-full_Revenue",
    "ifrs-full_RevenueFromContractsWithCustomers",
    "ifrs_Revenue",
]
OPERATING_INCOME_CANDIDATES = [
    "dart_OperatingIncomeLoss",
    "ifrs-full_ProfitLossFromOperatingActivities",
    "ifrs_OperatingIncomeLoss",
]


def load_api_key() -> str:
    key = os.getenv("OPENDART_KEY")
    if not key:
        raise RuntimeError(".env에 OPENDART_KEY가 설정되어 있지 않습니다")
    return key


# ── corpCode.xml: 종목코드 <-> corp_code 매핑 ────────────────────────────────

def download_corp_code_map(api_key: str, force: bool = False) -> pd.DataFrame:
    """corpCode.xml(zip) 다운로드 + 캐싱. 상장사 신규/변경분만 가끔 바뀌므로
    force=False면 캐시 존재 시 재다운로드하지 않음."""
    if CORP_CODE_CACHE.exists() and not force:
        return pd.read_parquet(CORP_CODE_CACHE)

    resp = requests.get(CORP_CODE_URL, params={"crtfc_key": api_key}, timeout=60)
    resp.raise_for_status()
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])

    root = ElementTree.fromstring(xml_bytes)
    rows = []
    for el in root.findall("list"):
        stock_code = (el.findtext("stock_code") or "").strip()
        if not stock_code:
            continue  # 비상장 법인 제외 (종목코드 없는 항목)
        rows.append({
            "corp_code": el.findtext("corp_code"),
            "corp_name": el.findtext("corp_name"),
            "code": stock_code,
            "modify_date": el.findtext("modify_date"),
        })
    df = pd.DataFrame(rows)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CORP_CODE_CACHE, index=False)
    print(f"[corpCode] {len(df)}개 상장사 매핑 캐싱 완료 → {CORP_CODE_CACHE}")
    return df


# ── fnlttSinglAcntAll 호출 + 계정 추출 ───────────────────────────────────────

def _pick_account(items: list[dict], candidates: list[str]) -> tuple[Optional[float], Optional[str]]:
    """account_id 후보 리스트를 우선순위로 매칭해 thstrm_amount(당기 누적 금액) 추출."""
    by_id = {it.get("account_id"): it for it in items}
    for cand in candidates:
        it = by_id.get(cand)
        if it and it.get("thstrm_amount"):
            raw = str(it["thstrm_amount"]).replace(",", "").strip()
            if raw and raw not in ("-",):
                try:
                    return float(raw), cand
                except ValueError:
                    continue
    return None, None


def _raw_cache_path(corp_code: str, year: int, reprt_code: str, fs_div: str) -> Path:
    return RAW_CACHE_DIR / f"{corp_code}_{year}_{reprt_code}_{fs_div}.json"


def fetch_financials_raw(api_key: str, corp_code: str, year: int, reprt_code: str,
                          fs_div: str = "CFS") -> Optional[dict]:
    """fnlttSinglAcntAll 1회 호출 (로컬 캐시 우선). 응답 status != '000'이면 None."""
    cache_path = _raw_cache_path(corp_code, year, reprt_code, fs_div)
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        resp = requests.get(FINSTATE_URL, params={
            "crtfc_key": api_key, "corp_code": corp_code, "bsns_year": str(year),
            "reprt_code": reprt_code, "fs_div": fs_div,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        time.sleep(REQUEST_SLEEP_SEC)

    if data.get("status") != "000":
        return None
    return data


def fetch_financials_row(api_key: str, code: str, corp_code: str, corp_name: str,
                          year: int, reprt_code: str) -> Optional[dict]:
    """연결(CFS) 우선 조회, 연결 미제출 기업은 별도(OFS)로 폴백."""
    for fs_div in ("CFS", "OFS"):
        data = fetch_financials_raw(api_key, corp_code, year, reprt_code, fs_div)
        if data is None:
            continue
        items = data["list"]
        revenue, rev_id = _pick_account(items, REVENUE_CANDIDATES)
        op_income, oi_id = _pick_account(items, OPERATING_INCOME_CANDIDATES)
        if revenue is None and op_income is None:
            continue  # 표준계정 둘 다 없음 — 다음 fs_div(OFS) 시도

        rcept_no = items[0].get("rcept_no") if items else None
        # DART rcept_no 표준 형식 = 접수연월일(8자리) + 일련번호(6자리) — 별도 API
        # 호출 없이 그대로 룩어헤드 방지 기준일(공시 접수일)로 사용 가능
        rcept_dt = rcept_no[:8] if rcept_no and len(rcept_no) >= 8 else None
        return {
            "code": code, "corp_code": corp_code, "corp_name": corp_name,
            "bsns_year": year, "reprt_code": reprt_code, "fs_div": fs_div,
            "rcept_no": rcept_no, "rcept_dt": rcept_dt,
            "revenue": revenue, "revenue_account_id": rev_id,
            "operating_income": op_income, "oi_account_id": oi_id,
        }
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes-file", required=True, help="종목코드 목록 (1행 1코드)")
    parser.add_argument("--start-year", type=int, default=2014,
                        help="수집 시작 연도 (YoY 기준연도 확보를 위해 분석 시작연도-1)")
    parser.add_argument("--end-year", type=int, default=2019)
    parser.add_argument("--resume", action="store_true", help="기존 출력에 이미 있는 (code,year,reprt_code)는 스킵")
    args = parser.parse_args()

    api_key = load_api_key()
    codes = [c.strip() for c in Path(args.codes_file).read_text(encoding="utf-8").splitlines() if c.strip()]
    print(f"대상 종목수: {len(codes)}")

    corp_map = download_corp_code_map(api_key)
    corp_map = corp_map[corp_map["code"].isin(codes)]
    missing_corp = set(codes) - set(corp_map["code"])
    print(f"corp_code 매핑: {len(corp_map)}/{len(codes)} (미매핑 {len(missing_corp)}건)")

    existing = pd.DataFrame()
    if args.resume and OUTPUT_PATH.exists():
        existing = pd.read_parquet(OUTPUT_PATH)

    rows = []
    missing_rows = []
    years = range(args.start_year, args.end_year + 1)
    total = len(corp_map) * len(years) * len(REPRT_CODES)
    done = 0
    for _, corp in corp_map.iterrows():
        for year in years:
            for reprt_code in REPRT_CODES:
                done += 1
                if args.resume and not existing.empty:
                    dup = existing[(existing["code"] == corp["code"]) & (existing["bsns_year"] == year)
                                    & (existing["reprt_code"] == reprt_code)]
                    if not dup.empty:
                        continue
                try:
                    row = fetch_financials_row(api_key, corp["code"], corp["corp_code"], corp["corp_name"],
                                                year, reprt_code)
                except requests.RequestException as e:
                    print(f"  [경고] {corp['code']} {year} {reprt_code} 요청 실패: {e}")
                    missing_rows.append((corp["code"], corp["corp_name"], year, reprt_code, str(e)))
                    continue
                if row is None:
                    missing_rows.append((corp["code"], corp["corp_name"], year, reprt_code, "표준계정 없음/미제출"))
                    continue
                rows.append(row)
                if done % 200 == 0:
                    print(f"  진행 {done}/{total} (수집 {len(rows)}건, 결측 {len(missing_rows)}건)")

    for code in missing_corp:
        missing_rows.append((code, None, None, None, "corp_code 매핑 없음"))

    if missing_rows:
        MISSING_LOG.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(missing_rows, columns=["code", "corp_name", "bsns_year", "reprt_code", "reason"]) \
          .to_csv(MISSING_LOG, index=False, encoding="utf-8-sig", mode="a",
                  header=not MISSING_LOG.exists())
        print(f"[결측 기록] {len(missing_rows)}건 → {MISSING_LOG}")

    if not rows:
        print("[완료] 수집된 행 없음")
        return

    new_df = pd.DataFrame(rows)
    if not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["code", "bsns_year", "reprt_code", "fs_div"], keep="last")
    else:
        combined = new_df

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(OUTPUT_PATH, index=False)
    print(f"\n[완료] {len(new_df)}건 신규 수집, 누적 {len(combined)}건 → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

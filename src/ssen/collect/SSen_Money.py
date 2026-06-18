import os
import time
import requests
import pandas as pd
from datetime import datetime
from urllib.parse import unquote
from dotenv import load_dotenv

load_dotenv()

START_DATE = "20260530"
END_DATE   = "20260610"


def load_service_key() -> str:
    key = os.getenv("DATA_GO_KR_KEY")
    if not key:
        raise RuntimeError(".env에 DATA_GO_KR_KEY가 설정되어 있지 않습니다 (docs/api_collection.md 참조)")
    return unquote(key)


SERVICE_KEY = load_service_key()

URL = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"

def fetch_all_by_date(yyyymmdd: str, market: str) -> pd.DataFrame:
    """KOSDAQ 또는 KOSPI 전체 종목 시세 조회"""
    params = {
        "serviceKey": SERVICE_KEY,
        "resultType": "json",
        "numOfRows": 5000,
        "pageNo": 1,
        "basDt": yyyymmdd,
        "mrktCls": market,
    }
    r = requests.get(URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    body = (data.get("response", {}) or {}).get("body", {}) or {}
    items = (body.get("items", {}) or {}).get("item", [])

    if not items:
        return pd.DataFrame()
    if isinstance(items, dict):
        items = [items]

    return pd.DataFrame(items)


def to_top50_format(df: pd.DataFrame, yyyymmdd: str, market: str) -> pd.DataFrame:
    rename_map = {
        "srtnCd": "종목코드",
        "itmsNm": "종목명",
        "mkp": "시작일기준가",
        "clpr": "종료일종가",
        "vs": "대비",
        "fltRt": "등락률",
        "trqu": "거래량_합계",
        "trPrc": "거래대금_합계",
    }
    df = df.rename(columns=rename_map)

    for c in ["시작일기준가", "종료일종가", "대비", "등락률", "거래량_합계", "거래대금_합계"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if "거래대금_합계" not in df.columns:
        return pd.DataFrame()

    df = df.dropna(subset=["거래대금_합계"])
    df = df.sort_values("거래대금_합계", ascending=False).head(50).reset_index(drop=True)

    date_val = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    df.insert(0, "날짜", date_val)
    df.insert(1, "순위", range(1, len(df) + 1))
    df.insert(4, "시장구분", market)

    # 중복 컬럼 추가
    df["거래량_일평균"] = df["거래량_합계"]
    df["거래대금_일평균"] = df["거래대금_합계"]

    cols = ["날짜", "순위", "종목코드", "종목명", "시장구분",
            "시작일기준가", "종료일종가", "대비", "등락률",
            "거래량_합계", "거래량_일평균", "거래대금_합계", "거래대금_일평균"]
    cols = [c for c in cols if c in df.columns]
    return df[cols]


def main():
    days = pd.date_range(
        start=pd.to_datetime(START_DATE),
        end=pd.to_datetime(END_DATE),
        freq="B"
    ).strftime("%Y%m%d").tolist()

    all_daily = []

    for d in days:
        print(f"[{d}] 수집 중...")
        day_frames = []

        for market in ["KOSPI", "KOSDAQ"]:
            try:
                raw = fetch_all_by_date(d, market)
                if raw.empty:
                    print(f"  - {market} 데이터 없음")
                    continue

                top50 = to_top50_format(raw, d, market)
                if not top50.empty:
                    day_frames.append(top50)

                time.sleep(0.15)

            except Exception as e:
                print(f"  - {market} 실패({d}): {e}")
                time.sleep(0.5)

        if day_frames:
            # 날짜별로 KOSDAQ → KOSPI 순서 유지
            all_daily.append(pd.concat(day_frames, ignore_index=True))

    if not all_daily:
        print("수집된 데이터가 없습니다.")
        return

    final_df = pd.concat(all_daily, ignore_index=True)

    out = f"거래대금_상위50_{START_DATE}_{END_DATE}.xlsx"

    with pd.ExcelWriter(out, engine="openpyxl", datetime_format="YYYY-MM-DD") as writer:
        final_df.to_excel(writer, index=False, sheet_name="데이터")

        # 날짜 열 서식 명시적으로 지정
        ws = writer.sheets["데이터"]
        from openpyxl.styles import numbers
        for cell in ws["A"][1:]:  # 헤더 제외
            cell.number_format = "YYYY-MM-DD"

    print(f"==> 저장 완료: {out}")


if __name__ == "__main__":
    main()
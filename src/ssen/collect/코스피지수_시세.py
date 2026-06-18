import os
import requests
import pandas as pd
from urllib.parse import unquote
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 사용자 설정 정보
# ==========================================
START_DATE = "20260530"
END_DATE = "20260610"
OUTPUT_FILE_XLSX = "market_index_prices.xlsx"
OUTPUT_FILE_CSV = "market_index_prices.csv"

URL = "https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getStockMarketIndex"
INDICES = ["코스피", "코스닥"]
# ==========================================


def _normalize_items(items):
    if items is None:
        return []
    return items if isinstance(items, list) else [items]


def load_service_key():
    key = os.getenv("DATA_GO_KR_KEY")
    if not key:
        raise RuntimeError(".env에 DATA_GO_KR_KEY가 설정되어 있지 않습니다 (docs/api_collection.md 참조)")
    return unquote(key)


def fetch_index_prices(service_key: str, idx_name: str, begin_date: str, end_date: str) -> pd.DataFrame:
    params = {
        "serviceKey": service_key,
        "resultType": "json",
        "numOfRows": 1000,
        "pageNo": 1,
        "idxNm": idx_name,
        "beginBasDt": begin_date,
        "endBasDt": end_date,
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    response = requests.get(URL, params=params, headers=headers, timeout=30)
    if response.status_code != 200:
        print(f"[오류] {idx_name} 조회 중 HTTP {response.status_code} 에러가 발생했습니다.")
        print("요청 URL:", response.url)
        print("응답 헤더:", response.headers)
        print("응답 본문:", response.text)
        response.raise_for_status()

    data = response.json()
    items = data.get("response", {}).get("body", {}).get("items", {}).get("item")
    rows = _normalize_items(items)
    if not rows:
        print(f"[경고] {idx_name} 데이터가 존재하지 않습니다. 기간: {begin_date} ~ {end_date}")
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # KospiPrice 스키마에 맞게 영문 컬럼명으로 변환
    df = df.rename(columns={
        "basDt": "Date",
        "idxNm": "지수명",
        "mkp": "Open",
        "hipr": "High",
        "lopr": "Low",
        "clpr": "Close",
        "trqu": "Volume",
        "vs": "전일대비",
        "fltRt": "Increase rate",
    })

    desired_columns = ["Date", "지수명", "Open", "High", "Low", "Close", "Volume", "전일대비", "Increase rate"]
    df = df[[col for col in desired_columns if col in df.columns]]

    for col in ["Open", "High", "Low", "Close", "Volume", "전일대비", "Increase rate"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
        df = df.sort_values("Date").reset_index(drop=True)

    return df


def main():
    service_key = load_service_key()
    print(f"코스피·코스닥 지수 시세 수집 시작: {START_DATE} ~ {END_DATE}")

    frames = []
    for idx_name in INDICES:
        try:
            df = fetch_index_prices(service_key, idx_name, START_DATE, END_DATE)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            print(f"[오류] {idx_name} 데이터를 불러오는 중 예외가 발생했습니다: {e}")

    if not frames:
        print("[종료] 유효한 지수 데이터가 없어서 파일을 생성하지 않습니다.")
        return

    result = pd.concat(frames, ignore_index=True)
    result = result[[c for c in [
        "Date", "지수명", "Open", "High", "Low", "Close", "Volume", "전일대비", "Increase rate"
    ] if c in result.columns]]

    result.to_csv(OUTPUT_FILE_CSV, index=False, encoding="utf-8-sig")
    try:
        result.to_excel(OUTPUT_FILE_XLSX, index=False)
        print(f"\n[완료] 엑셀 파일로 저장되었습니다: {OUTPUT_FILE_XLSX}")
    except Exception as e:
        print(f"[경고] 엑셀 파일 저장에 실패했습니다: {e}")
        print("CSV 파일로는 저장되었습니다:", OUTPUT_FILE_CSV)
        return

    print(f"총 {len(result)}개 데이터가 생성되었습니다. CSV: {OUTPUT_FILE_CSV}, XLSX: {OUTPUT_FILE_XLSX}")
    print(result.head())


if __name__ == "__main__":
    main()

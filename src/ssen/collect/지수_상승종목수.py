import os
import requests
import pandas as pd
import time
import numpy as np
from datetime import datetime, timedelta
from urllib.parse import unquote
from dotenv import load_dotenv

load_dotenv()

START_DATE = "20260530"
END_DATE = "20260610"
CHUNK_DAYS = 5
URL = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"


def load_service_key() -> str:
    key = os.getenv("DATA_GO_KR_KEY")
    if not key:
        raise RuntimeError(".env에 DATA_GO_KR_KEY가 설정되어 있지 않습니다 (docs/api_collection.md 참조)")
    return key


DATA_GO_KR_SERVICE_KEY = load_service_key()

def get_date_chunks(start_date_str, end_date_str, chunk_size):
    start_dt = datetime.strptime(start_date_str, "%Y%m%d")
    end_dt = datetime.strptime(end_date_str, "%Y%m%d")
    chunks = []
    current_dt = start_dt
    while current_dt <= end_dt:
        chunk_end = current_dt + timedelta(days=chunk_size - 1)
        if chunk_end > end_dt:
            chunk_end = end_dt
        chunks.append((current_dt.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        current_dt = chunk_end + timedelta(days=1)
    return chunks

def get_market_adv_dec_stats(start_date, end_date, service_key, url):
    decoded_key = unquote(service_key)
    req_num_of_rows = 1000
    all_items = []
    markets = ["KOSPI", "KOSDAQ"]
    date_chunks = get_date_chunks(start_date, end_date, CHUNK_DAYS)

    for market in markets:
        print(f"\n[{market}] {start_date} ~ {end_date} 데이터 수집 시작...")
        for chunk_start, chunk_end in date_chunks:
            page_no = 1
            print(f"  -> 수집 구간: {chunk_start} ~ {chunk_end} 요청 중...")
            while True:
                params = {
                    "serviceKey": decoded_key,
                    "numOfRows": req_num_of_rows,
                    "pageNo": page_no,
                    "resultType": "json",
                    "mrktCls": market,
                    "beginBasDt": chunk_start,
                    "endBasDt": chunk_end
                }
                response = requests.get(url, params=params)
                if response.status_code != 200:
                    print(f"[{market}] API 요청 오류: HTTP {response.status_code}")
                    break
                try:
                    data = response.json()
                    body = data.get('response', {}).get('body', {})
                    if not body or not body.get('items'):
                        break
                    items = body['items'].get('item', [])
                    if isinstance(items, dict):
                        items = [items]
                    if not items:
                        break
                    all_items.extend(items)
                    total_count = int(body.get('totalCount', 0))
                    if page_no * req_num_of_rows >= total_count:
                        break
                    page_no += 1
                except Exception as e:
                    print(f"응답 데이터를 파싱하는 중 오류가 발생했습니다: {e}")
                    break
                time.sleep(0.5)

    if not all_items:
        print("수집된 데이터가 없습니다.")
        return pd.DataFrame()

    df = pd.DataFrame(all_items)

    if 'mrktCls' not in df.columns and 'mrktCtg' in df.columns:
        df = df.rename(columns={'mrktCtg': 'mrktCls'})

    required_cols = ['basDt', 'mrktCls', 'fltRt']
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        raise KeyError(f"응답 데이터에 필수 컬럼이 없습니다: {missing_cols}")

    df = df[required_cols].copy()
    df['fltRt'] = pd.to_numeric(df['fltRt'], errors='coerce')
    df['상승'] = df['fltRt'] > 0
    df['하락'] = df['fltRt'] < 0
    df['보합'] = df['fltRt'] == 0

    summary_df = df.groupby(['basDt', 'mrktCls'])[['상승', '하락', '보합']].sum().reset_index()
    summary_df.rename(columns={
        'basDt': '날짜',
        'mrktCls': '지수',
        '상승': '상승종목수',
        '하락': '하락종목수',
        '보합': '보합종목수'
    }, inplace=True)

    summary_df['하락 대비 상승비율'] = np.where(
        summary_df['하락종목수'] == 0,
        0.0,
        (summary_df['상승종목수'] / summary_df['하락종목수'] * 100)
    ).round(1)

    summary_df['날짜'] = pd.to_datetime(summary_df['날짜'], format="%Y%m%d", errors="coerce").dt.strftime('%Y-%m-%d')

    final_df = summary_df.sort_values(by=['날짜', '지수']).reset_index(drop=True)
    return final_df

if __name__ == "__main__":
    result_df = get_market_adv_dec_stats(START_DATE, END_DATE, DATA_GO_KR_SERVICE_KEY, URL)
    if not result_df.empty:
        print("\n[수집 완료 - 누락 방지 적용]")
        print(result_df.to_string(index=False))
        result_df.to_excel("market_stats_fixed.xlsx", index=False)
        print("\nmarket_stats_fixed.xlsx 파일로 저장이 완료되었습니다.")
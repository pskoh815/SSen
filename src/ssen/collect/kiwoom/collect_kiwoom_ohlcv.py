# -*- coding: utf-8 -*-
"""키움 OpenAPI+ 개별 종목 OHLCV 수집 (OPT10081 주식일봉차트조회요청).

반드시 32비트 Python으로 실행:
    py -3.9-32 collect_kiwoom_ohlcv.py --codes-file <종목코드 목록 txt, 1행 1코드>
                                        [--end YYYY-MM-DD] [--lookback-days 400]

대상 종목 목록은 64bit 쪽(daily_update.py)에서 fact_daily_stock 기준
"최근 250거래일 거래대금 상위 누적 종목"으로 계산해 파일로 넘겨준다
(daily_update.py의 universe 정의: RS_PERIOD_12M=250과 일치).

검증된 사실 (2026-06-17 실측, 원익IPS/240810):
  - 1회 호출 소요시간 0.45초, 600행(약 2.4년치) 반환 — 종목당 1회 호출로 충분
  - 거래대금 단위 = 백만원 (×1,000,000, opt10032와 동일 패턴 — 06-12 거래대금×100만
    =690.95억 vs data.go.kr 같은 날 거래대금 690.95억대와 같은 규모로 교차검증)
  - 거래량/가격은 raw 단위 (변환 불필요, data.go.kr과 동일)
  - 종목코드는 응답 0행만 채워지고 나머지는 공란, 종목명/시장구분은 응답에 없음
    → 출력 CSV에는 code/OHLCV만 담고, name/market은 daily_update.py가 dim_stock과
    join해서 보완
  - 호출 제한: 키움 개발가이드 1차 출처 "데이터 조회는 1초당 5회로 제한" + 수치
    비공개 "서버부하방지 제한" 별도 존재 → 안전마진으로 호출 간 0.3초 슬립 적용
    (이론상 약 3.3회/초로 운영)

2026-06-18 발견한 사고: 1242종목 백필 중 "중복 로그인" 경고가 발생, 키움이 네이티브
메시지박스를 띄우며 COM 이벤트 루프가 멈춰 block_request()가 영원히 반환하지 않는
행(hang)이 발생(CPU 0%, 54분간 무응답). 이 스크립트는 전 종목을 메모리에만 쌓아두고
루프 종료 후 한 번에 CSV로 쓰는 구조라, 강제종료 시 진행분이 전부 날아갔음(디스크에는
아무 흔적도 안 남아 다행히 손상은 없었지만 처음부터 재수집해야 했음). 재발 방지로
다음 두 가지를 추가:
  1. 종목 K개(기본 20)마다 CSV에 append 저장 — 강제종료돼도 그 시점까지 진행분은 보존
  2. 워치독 타이머 — 정상 호출은 1종목당 1초 내외인데, WATCHDOG_TIMEOUT_SEC(기본 30초)
     동안 진행이 없으면(= block_request가 멈춤) 강제로 프로세스 종료(os._exit) —
     무한 행 대신 "어디까지 했는지"를 즉시 알 수 있고, 재실행 시 --resume으로 이어서 가능
"""
import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from pykiwoom.kiwoom import Kiwoom

from _watchdog import Watchdog  # 4개 키움 수집 스크립트 공용 (같은 디렉터리)

OUTPUT_DIR = Path(r"C:\MyClaude\ssen-dashboard\data\incoming\kiwoom")
CALL_INTERVAL_SEC = 0.3  # 1초당 5회 제한 대비 안전마진 (약 3.3회/초)
SAVE_EVERY_N = 20        # 이만큼 처리할 때마다 CSV에 append 저장
WATCHDOG_TIMEOUT_SEC = 30  # 이 시간 동안 진행 없으면 행(hang)으로 간주, 강제 종료
# 워치독 발동 시 지금까지 저장된 분량은 디스크에 보존되며, --resume으로 이어서 재시작 가능


def fetch_stock_daily(kiwoom: Kiwoom, code: str, end_date: date) -> pd.DataFrame:
    """OPT10081로 종목 1개의 과거 일봉 일괄 조회."""
    df = kiwoom.block_request(
        "opt10081",
        종목코드=code,
        기준일자=end_date.strftime("%Y%m%d"),
        수정주가구분="1",
        output="주식일봉차트조회",
        next=0,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[df["일자"].astype(str).str.len() == 8].copy()
    if df.empty:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["일자"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    out["code"] = code
    for col, raw in [("open", "시가"), ("high", "고가"), ("low", "저가"), ("close", "현재가")]:
        out[col] = df[raw].astype(float)
    out["volume"] = df["거래량"].astype(float)
    # 거래대금 단위 변환: 백만원 → 원 (docstring 실측 검증 참조)
    out["amount"] = df["거래대금"].astype(float) * 1_000_000
    return out


def _existing_codes(out_path: Path) -> set[str]:
    if not out_path.exists():
        return set()
    try:
        df = pd.read_csv(out_path, encoding="utf-8-sig", dtype={"code": str}, usecols=["code"])
        return set(df["code"].unique())
    except Exception:
        return set()


def _append_to_csv(frames: list[pd.DataFrame], out_path: Path) -> None:
    if not frames:
        return
    combined = pd.concat(frames, ignore_index=True)
    write_header = not out_path.exists()
    combined.to_csv(out_path, index=False, encoding="utf-8-sig",
                    mode="a", header=write_header)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes-file", required=True, help="종목코드 목록 (1행 1코드)")
    parser.add_argument("--end", help="기준일자 YYYY-MM-DD (생략 시 오늘)")
    parser.add_argument("--lookback-days", type=int, default=400,
                        help="결과를 이 일수 이내로 잘라서 저장 (기본 400일 — RS 250거래일+버퍼)")
    parser.add_argument("--resume", action="store_true",
                        help="기존 출력 CSV에 이미 있는 종목은 스킵하고 이어서 수집")
    args = parser.parse_args()

    end_date = date.fromisoformat(args.end) if args.end else date.today()
    cutoff = end_date - timedelta(days=args.lookback_days)

    codes = [line.strip() for line in Path(args.codes_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not codes:
        print("[오류] 종목코드 목록이 비어있음")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"kiwoom_ohlcv_{end_date.strftime('%Y%m%d')}.csv"

    if args.resume:
        done = _existing_codes(out_path)
        before = len(codes)
        codes = [c for c in codes if c not in done]
        print(f"[resume] 기존 {len(done)}종목 완료 확인 → {before}종목 중 {len(codes)}종목 남음")
    else:
        if out_path.exists():
            print(f"[경고] 기존 출력 파일 삭제 후 새로 시작: {out_path}")
            out_path.unlink()

    print(f"대상 종목수: {len(codes)}")
    if not codes:
        print("[완료] 처리할 종목이 없음 (이미 전부 완료됨)")
        return

    kiwoom = Kiwoom()
    kiwoom.CommConnect(block=True)
    if kiwoom.GetConnectState() != 1:
        print("[오류] 키움 OpenAPI 로그인 실패")
        sys.exit(1)
    print(f"[로그인 성공] 계좌: {kiwoom.GetLoginInfo('ACCNO')}")

    watchdog = Watchdog(WATCHDOG_TIMEOUT_SEC)
    watchdog.reset()

    frames: list[pd.DataFrame] = []
    failed = []
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        if kiwoom.GetConnectState() != 1:
            print(f"\n[오류] 키움 연결 끊김 감지 (종목 {i}/{len(codes)}, {code}) — "
                  f"지금까지 수집분 저장 후 종료")
            break
        try:
            df = fetch_stock_daily(kiwoom, code, end_date)
            if df.empty:
                failed.append(code)
            else:
                df = df[df["date"] >= str(cutoff)]
                frames.append(df)
        except Exception as e:
            print(f"  [경고] {code} 수집 실패: {e}")
            failed.append(code)
        watchdog.reset()  # 이번 종목 처리 완료 — 워치독 타이머 리셋

        if i % SAVE_EVERY_N == 0:
            _append_to_csv(frames, out_path)
            frames = []
        if i % 50 == 0:
            elapsed = time.time() - t0
            print(f"  진행 {i}/{len(codes)} ({elapsed:.0f}초 경과, 실패 {len(failed)}건)")
        time.sleep(CALL_INTERVAL_SEC)

    watchdog.stop()
    _append_to_csv(frames, out_path)  # 남은 분량 마지막 저장

    elapsed = time.time() - t0
    n_saved = len(_existing_codes(out_path))
    print(f"\n[완료] {len(codes)}종목 중 {n_saved}종목 누적 저장, 실패 {len(failed)}건, "
          f"{elapsed:.0f}초 소요 → {out_path}")
    if failed:
        print(f"  실패 종목: {failed[:20]}{'...' if len(failed) > 20 else ''}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""ADR(OPT20009) 확정 시점 검증.

같은 거래일에 15:35와 16:30 두 번 호출해 상승/하락/보합 종목수가 동일한지 비교한다.
동일하면 장마감 직후(15:40) 수집을 그대로 사용, 다르면 시간외 단일가(16:00) 반영 후로
수집 시각을 늦춰야 한다는 결론을 daily_update.py의 ADR 수집 시각 확정에 사용한다.

32비트 전용. Usage:
    py -3.9-32 verify_adr_timing.py capture --label 1535
    py -3.9-32 verify_adr_timing.py capture --label 1630
    py -3.9-32 verify_adr_timing.py compare
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

OUTPUT_DIR = Path(r"C:\MyClaude\ssen-dashboard\data\adr_verify")
LOG_PATH = Path(r"C:\MyClaude\ssen-dashboard\logs\adr_verify.log")
CONFIRMED_PATH = OUTPUT_DIR / "CONFIRMED.json"
FIELDS = ["상승종목수", "하락종목수", "보합종목수"]


def capture(label: str) -> None:
    from pykiwoom.kiwoom import Kiwoom
    from collect_kiwoom_adr import fetch_adr_today

    today = date.today()
    kiwoom = Kiwoom()
    kiwoom.CommConnect(block=True)
    if kiwoom.GetConnectState() != 1:
        print("[오류] 키움 OpenAPI 로그인 실패")
        sys.exit(1)

    df = fetch_adr_today(kiwoom, today)
    if df.empty:
        print("[오류] 수집 결과 비어있음")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": str(today),
        "label": label,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "rows": df.to_dict(orient="records"),
    }
    out_path = OUTPUT_DIR / f"{today.strftime('%Y%m%d')}_{label}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[완료] {label} 캡처 저장 → {out_path}")
    print(df)


def compare() -> None:
    today = date.today().strftime("%Y%m%d")
    p1535 = OUTPUT_DIR / f"{today}_1535.json"
    p1630 = OUTPUT_DIR / f"{today}_1630.json"

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not p1535.exists() or not p1630.exists():
        msg = f"[대기] 비교 불가 — 1535={p1535.exists()} 1630={p1630.exists()}"
        print(msg)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
        sys.exit(0)

    d1535 = json.load(open(p1535, encoding="utf-8"))
    d1630 = json.load(open(p1630, encoding="utf-8"))
    rows1535 = {r["지수"]: r for r in d1535["rows"]}
    rows1630 = {r["지수"]: r for r in d1630["rows"]}

    identical = True
    lines = [f"=== ADR 확정시점 비교 ({today}) ==="]
    for market in sorted(set(rows1535) | set(rows1630)):
        r1, r2 = rows1535.get(market, {}), rows1630.get(market, {})
        diffs = {f: (r1.get(f), r2.get(f)) for f in FIELDS if r1.get(f) != r2.get(f)}
        if diffs:
            identical = False
            lines.append(f"  {market}: 차이 있음 {diffs}")
        else:
            lines.append(f"  {market}: 동일 ({ {f: r1.get(f) for f in FIELDS} })")

    verdict = "동일 → 15:40 수집 그대로 진행" if identical else "차이 있음 → 16:00 이후로 수집 시각 조정 필요"
    lines.append(f"결론: {verdict}")
    output = "\n".join(lines)
    print(output)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(output + "\n")

    # 검증 완료 표시 — 이후 collect_kiwoom_adr.py가 is_verified=True를 붙이기 시작함.
    # 기존에 is_verified=False로 쌓인 과거 행은 daily_update.reconcile_adr()로 별도 정리.
    with open(CONFIRMED_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "confirmed_at": datetime.now().isoformat(timespec="seconds"),
            "checked_date": today,
            "identical": identical,
            "verdict": verdict,
            "recommended_collect_time": "15:40" if identical else "16:00 이후",
        }, f, ensure_ascii=False, indent=2)
    print(f"[확정] CONFIRMED.json 기록 → {CONFIRMED_PATH}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--label", required=True, choices=["1535", "1630"])
    sub.add_parser("compare")
    args = parser.parse_args()

    if args.cmd == "capture":
        capture(args.label)
    else:
        compare()


if __name__ == "__main__":
    main()

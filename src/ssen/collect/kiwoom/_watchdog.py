# -*- coding: utf-8 -*-
"""32bit 키움 수집 스크립트 공용 워치독.

block_request()/CommConnect()가 키움 측 팝업(중복 로그인 경고 등)이나 COM
행(hang)으로 영원히 안 끝나는 경우를 대비 — 일정 시간 진행(reset 호출)이
없으면 프로세스를 강제 종료(os._exit)한다.

2026-06-17 실측: 1242종목 OHLCV 백필 중 중복 로그인 경고로 block_request가
54분간 무응답(CPU 0%)으로 멈춤 — collect_kiwoom_ohlcv.py에 처음 도입.
2026-06-18: daily_collect의 거래대금/코스피시세/ADR 수집은 매번 몇 초~수십초
내로 끝나는 짧은 스크립트라 지금까지 워치독이 없었는데, "예상치 못한 지점에서
막힐 가능성"(이번엔 인코딩 에러였지만 다음은 또 다른 원인일 수 있음)에 대한
공통 안전장치로 money/kospi/adr 3개 스크립트에도 통일 적용.

각 스크립트와 같은 디렉터리에 있으므로 `from _watchdog import Watchdog`로
바로 임포트 가능(32bit 서브프로세스로 스크립트를 직접 실행하면 그 스크립트의
디렉터리가 sys.path[0]에 자동으로 들어감).
"""
import os
import sys
import threading
from typing import Optional


class Watchdog:
    def __init__(self, timeout_sec: float):
        self.timeout_sec = timeout_sec
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def _fire(self) -> None:
        print(f"\n[워치독] {self.timeout_sec:.0f}초 동안 진행 없음 — 행(hang) 의심, 강제 종료")
        sys.stdout.flush()
        os._exit(99)

    def reset(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.timeout_sec, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def stop(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()

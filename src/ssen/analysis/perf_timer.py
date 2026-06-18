# -*- coding: utf-8 -*-
"""§E12: /api/period/* 요청 단위 DB 쿼리 시간 누적 (스레드 로컬).

각 요청(스레드)에서 reset_db_timer() 호출 후 DB 쿼리를
timed_db_query()로 감싸면, get_db_time()으로 누적 ms를 조회할 수 있다.
"""
from __future__ import annotations

import threading
import time

_local = threading.local()


def reset_db_timer() -> None:
    _local.db_ms = 0.0


def get_db_time() -> float:
    return getattr(_local, "db_ms", 0.0)


class timed_db_query:
    """`with timed_db_query(): df = con.execute(sql).fetchdf()` — DB 쿼리 시간을 누적기에 더한다."""

    def __enter__(self) -> "timed_db_query":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        elapsed_ms = (time.perf_counter() - self._t0) * 1000
        _local.db_ms = getattr(_local, "db_ms", 0.0) + elapsed_ms

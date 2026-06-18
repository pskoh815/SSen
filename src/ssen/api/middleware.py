"""E6: 구조화 로그 미들웨어 + 인메모리 메트릭."""
from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("ssen.api")

# ── 인메모리 메트릭 (경량, 재시작 초기화) ────────────────────────────────────
_metrics: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
_request_count: dict[str, int] = defaultdict(int)
_error_count: dict[str, int] = defaultdict(int)


def record_latency(path: str, latency_ms: float) -> None:
    key = path.split("?")[0]  # query string 제거
    _metrics[key].append(latency_ms)
    _request_count[key] += 1


def record_error(path: str) -> None:
    key = path.split("?")[0]
    _error_count[key] += 1


def get_metrics() -> dict:
    import numpy as np
    result = {}
    for path, times in _metrics.items():
        if not times:
            continue
        arr = np.array(list(times))
        result[path] = {
            "request_count": _request_count[path],
            "error_count": _error_count.get(path, 0),
            "p50_ms": round(float(np.percentile(arr, 50)), 2),
            "p95_ms": round(float(np.percentile(arr, 95)), 2),
            "p99_ms": round(float(np.percentile(arr, 99)), 2),
            "avg_ms": round(float(arr.mean()), 2),
            "max_ms": round(float(arr.max()), 2),
        }
    return result


# ── 미들웨어 ─────────────────────────────────────────────────────────────────

class LoggingMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        req_id = str(uuid.uuid4())[:8]
        t0 = time.perf_counter()

        response = await call_next(request)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        path = request.url.path
        qs = str(request.url.query)

        record_latency(path, elapsed_ms)
        if response.status_code >= 400:
            record_error(path)

        log.info(
            '{"req_id":"%s","method":"%s","path":"%s","qs":"%s","status":%d,"ms":%.1f}',
            req_id, request.method, path, qs, response.status_code, elapsed_ms,
        )
        response.headers["X-Request-Id"] = req_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        return response

"""
E6 캐시 레이어.

우선순위:
  1. Redis (SSEN_REDIS_URL 환경변수 또는 redis://localhost:6379)
  2. TTLCache fallback (Redis 미연결 시 자동)

캐시 키 형식:
  ssen:{namespace}:{dataset_version}:{params_hash}

무효화:
  - dataset_version이 바뀌면 기존 키가 자동으로 미스 (자연 무효화)
  - update 완료 후 invalidate_prefix() 호출로 즉시 삭제 가능
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import threading
import time
from typing import Any, Optional

from cachetools import TTLCache

log = logging.getLogger("ssen.cache")

REDIS_URL   = os.environ.get("SSEN_REDIS_URL", "redis://localhost:6379")
CACHE_TTL   = int(os.environ.get("SSEN_CACHE_TTL", "300"))  # 5분
CACHE_MAX   = int(os.environ.get("SSEN_CACHE_MAX", "1024"))
KEY_PREFIX  = "ssen"

# ── Backend ───────────────────────────────────────────────────────────────────

_redis_client = None
_use_redis    = False
_ttl_cache: TTLCache = TTLCache(maxsize=CACHE_MAX, ttl=CACHE_TTL)
_ttl_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "sets": 0, "backend": "ttl"}


def init_redis() -> bool:
    """Redis 연결 시도. 성공 시 True, 실패 시 False (TTLCache fallback)."""
    global _redis_client, _use_redis
    try:
        import redis
        client = redis.from_url(REDIS_URL, socket_connect_timeout=1,
                                socket_timeout=1, decode_responses=False)
        client.ping()
        _redis_client = client
        _use_redis = True
        _stats["backend"] = "redis"
        log.info(f"Redis 연결 성공: {REDIS_URL}")
        return True
    except Exception as e:
        log.warning(f"Redis 연결 실패 ({e}), TTLCache fallback 사용")
        _use_redis = False
        return False


# ── Key helpers ───────────────────────────────────────────────────────────────

def _make_key(namespace: str, dataset_version: str, **kwargs) -> str:
    params_str = json.dumps(kwargs, sort_keys=True, default=str)
    params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]
    return f"{KEY_PREFIX}:{namespace}:{dataset_version}:{params_hash}"


# ── Public API ────────────────────────────────────────────────────────────────

def cache_get(namespace: str, dataset_version: str, **kwargs) -> Optional[Any]:
    key = _make_key(namespace, dataset_version, **kwargs)
    if _use_redis:
        raw = _redis_client.get(key)
        if raw is not None:
            _stats["hits"] += 1
            return pickle.loads(raw)
    else:
        with _ttl_lock:
            val = _ttl_cache.get(key)
            if val is not None:
                _stats["hits"] += 1
                return val
    _stats["misses"] += 1
    return None


def cache_set(namespace: str, dataset_version: str, value: Any, **kwargs) -> None:
    key = _make_key(namespace, dataset_version, **kwargs)
    if _use_redis:
        _redis_client.setex(key, CACHE_TTL, pickle.dumps(value))
    else:
        with _ttl_lock:
            _ttl_cache[key] = value
    _stats["sets"] += 1


def invalidate_prefix(prefix: str) -> int:
    """해당 prefix로 시작하는 캐시 키 삭제. 반환: 삭제 수."""
    deleted = 0
    full_prefix = f"{KEY_PREFIX}:{prefix}"
    if _use_redis:
        cursor = 0
        while True:
            cursor, keys = _redis_client.scan(cursor, match=f"{full_prefix}*", count=100)
            if keys:
                deleted += _redis_client.delete(*keys)
            if cursor == 0:
                break
    else:
        with _ttl_lock:
            to_del = [k for k in list(_ttl_cache.keys())
                      if isinstance(k, str) and k.startswith(full_prefix)]
            for k in to_del:
                _ttl_cache.pop(k, None)
            deleted = len(to_del)
    log.info(f"캐시 무효화: prefix={prefix}, 삭제={deleted}건")
    return deleted


def cache_stats() -> dict:
    size = len(_ttl_cache) if not _use_redis else -1
    if _use_redis:
        try:
            info = _redis_client.info("memory")
            size = _redis_client.dbsize()
        except Exception:
            pass
    return {
        "backend": _stats["backend"],
        "hits": _stats["hits"],
        "misses": _stats["misses"],
        "sets": _stats["sets"],
        "hit_rate_pct": round(_stats["hits"] / max(_stats["hits"] + _stats["misses"], 1) * 100, 1),
        "current_size": size,
        "ttl_sec": CACHE_TTL,
        "redis_url": REDIS_URL if _use_redis else None,
    }


def cache_clear() -> None:
    if _use_redis:
        _redis_client.flushdb()
    else:
        with _ttl_lock:
            _ttl_cache.clear()
    _stats.update({"hits": 0, "misses": 0, "sets": 0})

"""
Shared TTL cache for adapter fetch calls.

Prevents redundant API calls when the same symbol is queried multiple times
within a short window (e.g., multiple concurrent SSE connections or repeated
queries from the same user).

Usage
-----
    from adapters.cache import ttl_cached

    @ttl_cached(ttl=300)   # 5-minute TTL
    def expensive_fetch(symbol: str) -> SomeData:
        ...

The cache key is derived from the decorated function's name + all positional
and keyword arguments.  datetime / date arguments are truncated to the minute
so minor timestamp differences don't cause cache misses.

Thread safety
-------------
cachetools.TTLCache is NOT thread-safe by default.  We protect all access with
a threading.Lock so concurrent asyncio.to_thread() calls don't corrupt the
cache.
"""
from __future__ import annotations

import functools
import threading
from datetime import date, datetime
from typing import Any, Callable, TypeVar

from cachetools import TTLCache

F = TypeVar("F", bound=Callable[..., Any])

# One global lock for all TTL caches — contention is low (cache hits are fast).
_CACHE_LOCK = threading.Lock()

# Per-adapter cache instances keyed by (ttl, maxsize).
_CACHES: dict[tuple[int, int], TTLCache[Any, Any]] = {}


def _get_cache(ttl: int, maxsize: int) -> TTLCache[Any, Any]:
    key = (ttl, maxsize)
    if key not in _CACHES:
        _CACHES[key] = TTLCache(maxsize=maxsize, ttl=ttl)
    return _CACHES[key]


def _make_cache_key(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Build a string cache key from function name + arguments."""
    def _normalise(v: Any) -> str:
        if isinstance(v, datetime):
            # Truncate to minute so calls within the same minute share cache
            return v.strftime("%Y%m%d%H%M")
        if isinstance(v, date):
            return v.isoformat()
        return repr(v)

    parts = [fn.__qualname__]
    parts += [_normalise(a) for a in args]
    parts += [f"{k}={_normalise(v)}" for k, v in sorted(kwargs.items())]
    return "|".join(parts)


def ttl_cached(ttl: int = 300, maxsize: int = 256) -> Callable[[F], F]:
    """
    Decorator that caches the return value of a sync function for `ttl` seconds.

    Parameters
    ----------
    ttl     : Cache lifetime in seconds (default 300 = 5 minutes).
    maxsize : Maximum number of cached entries (default 256).
    """
    def decorator(fn: F) -> F:
        cache = _get_cache(ttl, maxsize)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = _make_cache_key(fn, args, kwargs)
            with _CACHE_LOCK:
                if key in cache:
                    return cache[key]
            result = fn(*args, **kwargs)
            with _CACHE_LOCK:
                cache[key] = result
            return result

        return wrapper  # type: ignore[return-value]
    return decorator

"""Per-client rate limiting (in-memory sliding window, cost based).

* Client = source IP (or the first X-Forwarded-For hop when TRUST_PROXY_HEADERS=true).
* Cost = number of distinct ~1 km cache cells in the request (a 4-vertex parcel usually costs 1),
  which mirrors the real upstream work. Budget = RATE_LIMIT_UNITS per RATE_LIMIT_WINDOW_S.
* State lives in process memory: with several workers/replicas each one enforces its own budget.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Iterable, Tuple

from fastapi import HTTPException, Request, Response

from .config import get_settings


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    cost: int
    reset_s: float        # seconds until the whole budget is restored
    retry_after_s: float  # seconds until this request would fit (0 when allowed)


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic):
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self._clock = clock
        self._events: Dict[str, Deque[Tuple[float, int]]] = {}
        self._used: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def hit(self, key: str, cost: int = 1) -> Decision:
        cost = max(1, min(int(cost), self.limit))
        with self._lock:
            now = self._clock()
            self._calls += 1
            if self._calls % 1000 == 0:
                self._purge(now)
            events = self._events.setdefault(key, deque())
            used = self._evict(key, events, now)

            if used + cost <= self.limit:
                events.append((now, cost))
                self._used[key] = used + cost
                return Decision(True, self.limit, self.limit - used - cost, cost, self.window_s, 0.0)

            needed, freed, retry = used + cost - self.limit, 0, self.window_s
            for ts, c in events:
                freed += c
                if freed >= needed:
                    retry = max(0.0, ts + self.window_s - now)
                    break
            reset = max(0.0, events[-1][0] + self.window_s - now) if events else 0.0
            return Decision(False, self.limit, max(0, self.limit - used), cost, reset, retry)

    def _evict(self, key: str, events: Deque[Tuple[float, int]], now: float) -> int:
        used = self._used.get(key, 0)
        while events and events[0][0] <= now - self.window_s:
            used -= events.popleft()[1]
        self._used[key] = used
        return used

    def _purge(self, now: float) -> None:
        for key in list(self._events):
            self._evict(key, self._events[key], now)
            if not self._events[key]:
                self._events.pop(key, None)
                self._used.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._used.clear()


_cfg = get_settings()
limiter = SlidingWindowLimiter(_cfg.rate_limit_units, _cfg.rate_limit_window_s)


def client_id(request: Request) -> str:
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def distinct_cells(points: Iterable[Tuple[float, float]]) -> int:
    d = get_settings().cache_decimals
    return len({(round(lat, d), round(lon, d)) for lat, lon in points})  # same key as services.forecast.cell_of


def _headers(dec: Decision) -> Dict[str, str]:
    return {
        "X-RateLimit-Limit": str(dec.limit),
        "X-RateLimit-Remaining": str(dec.remaining),
        "X-RateLimit-Reset": str(math.ceil(dec.reset_s)),
        "X-RateLimit-Cost": str(dec.cost),
    }


def enforce(request: Request, response: Response, cost: int = 1) -> None:
    """Charge `cost` units to the caller. Raises HTTP 429 when the budget is exhausted."""
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return
    client = client_id(request)
    if client in settings.rate_limit_exempt_ips:
        return
    dec = limiter.hit(client, cost)
    headers = _headers(dec)
    if not dec.allowed:
        headers["Retry-After"] = str(max(1, math.ceil(dec.retry_after_s)))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {dec.limit} units per {int(limiter.window_s)} s. "
                   f"Retry in {headers['Retry-After']} s.",
            headers=headers,
        )
    response.headers.update(headers)

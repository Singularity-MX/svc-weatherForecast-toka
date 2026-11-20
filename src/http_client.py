"""Shared HTTP client (retries, concurrency cap, normalized errors)."""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import get_settings

log = logging.getLogger("toka.http")


class UpstreamError(Exception):
    """Failure while calling an external provider (Open-Meteo, NASA POWER)."""

    def __init__(self, provider: str, status: Optional[int], detail: str):
        self.provider = provider
        self.status = status
        self.detail = detail
        super().__init__(f"{provider}: {detail}" + (f" (HTTP {status})" if status else ""))


_session: Optional[requests.Session] = None
_session_lock = threading.Lock()
_semaphore: Optional[threading.BoundedSemaphore] = None


def _get_session() -> requests.Session:
    global _session, _semaphore
    with _session_lock:
        if _session is None:
            s = requests.Session()
            retry = Retry(
                total=2,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET",),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20))
            s.headers.update({"User-Agent": "svc-weatherForecast-toka/2.1"})
            _session = s
            _semaphore = threading.BoundedSemaphore(get_settings().max_upstream_concurrency)
        return _session


def _reason(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("reason") or body.get("message") or body)[:300]
    except ValueError:
        pass
    return (resp.text or resp.reason or "error")[:300]


def get_json(url: str, params: Dict[str, Any], provider: str) -> Dict[str, Any]:
    session = _get_session()
    assert _semaphore is not None
    with _semaphore:
        try:
            resp = session.get(url, params=params, timeout=get_settings().http_timeout_s)
        except requests.RequestException as e:
            raise UpstreamError(provider, None, f"no response ({e.__class__.__name__})") from e

    if resp.status_code >= 400:
        raise UpstreamError(provider, resp.status_code, _reason(resp))
    try:
        data = resp.json()
    except ValueError as e:
        raise UpstreamError(provider, resp.status_code, "response is not JSON") from e
    if isinstance(data, dict) and data.get("error") is True:
        raise UpstreamError(provider, resp.status_code, str(data.get("reason", "error")))
    return data

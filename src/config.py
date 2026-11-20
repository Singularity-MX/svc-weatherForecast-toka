"""Central configuration (environment variables with sensible defaults)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Tuple

VERSION = "3.0.0"
SERVICE_NAME = "svc-weatherForecast-toka"


def _tuple(name: str, default: str) -> Tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- Server ---------------------------------------------------------------
    port: int = 5000

    # --- Providers ------------------------------------------------------------
    open_meteo_api_key: str = ""

    # --- Horizon 1: short term (ensembles) --------------------------------------
    ensemble_models: Tuple[str, ...] = ("ecmwf_ifs025", "ecmwf_aifs025", "google_weathernext2")
    short_term_days: int = 15
    rain_threshold_mm: float = 1.0
    heavy_rain_mm: float = 20.0          # "extreme" day thresholds (generic; crop-specific ones belong to the recommender)
    frost_c: float = 0.0                 # Tmin <= frost_c
    heat_c: float = 35.0                 # Tmax >= heat_c

    # --- Horizon 2: seasonal ------------------------------------------------------
    seasonal_weekly_days: int = 46       # EC46
    seasonal_monthly_days: int = 210     # SEAS5 (~7 months)
    temp_normal_band_c: float = 0.5      # |anomaly| <= band  => "normal"
    precip_normal_band_pct: float = 20.0
    seasonal_confidence: str = "low"

    # --- Cache / network ----------------------------------------------------------
    ttl_short_term_s: int = 3600
    ttl_seasonal_s: int = 6 * 3600
    cache_decimals: int = 2              # 0.01 deg ~ 1.1 km
    http_timeout_s: float = 30.0
    max_workers: int = 4
    max_upstream_concurrency: int = 8

    # --- Rate limiting --------------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_units: int = 120          # units per window; 1 unit = 1 distinct ~1 km cell
    rate_limit_window_s: int = 60
    trust_proxy_headers: bool = False    # honour X-Forwarded-For (only behind a trusted proxy)
    rate_limit_exempt_ips: Tuple[str, ...] = ()

    # --- Derived URLs -------------------------------------------------------------
    @property
    def ensemble_url(self) -> str:
        host = "customer-ensemble-api" if self.open_meteo_api_key else "ensemble-api"
        return f"https://{host}.open-meteo.com/v1/ensemble"

    @property
    def seasonal_url(self) -> str:
        host = "customer-seasonal-api" if self.open_meteo_api_key else "seasonal-api"
        return f"https://{host}.open-meteo.com/v1/seasonal"

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls
        return cls(
            port=int(_num("PORT", d.port)),
            open_meteo_api_key=os.getenv("OPEN_METEO_API_KEY", ""),
            ensemble_models=_tuple("ENSEMBLE_MODELS", ",".join(d.ensemble_models)),
            short_term_days=int(_num("SHORT_TERM_DAYS", d.short_term_days)),
            rain_threshold_mm=_num("RAIN_THRESHOLD_MM", d.rain_threshold_mm),
            heavy_rain_mm=_num("HEAVY_RAIN_MM", d.heavy_rain_mm),
            frost_c=_num("FROST_C", d.frost_c),
            heat_c=_num("HEAT_C", d.heat_c),
            temp_normal_band_c=_num("TEMP_NORMAL_BAND_C", d.temp_normal_band_c),
            precip_normal_band_pct=_num("PRECIP_NORMAL_BAND_PCT", d.precip_normal_band_pct),
            seasonal_confidence=os.getenv("SEASONAL_CONFIDENCE", d.seasonal_confidence),
            ttl_short_term_s=int(_num("TTL_SHORT_TERM_S", d.ttl_short_term_s)),
            ttl_seasonal_s=int(_num("TTL_SEASONAL_S", d.ttl_seasonal_s)),
            http_timeout_s=_num("HTTP_TIMEOUT_S", d.http_timeout_s),
            max_workers=int(_num("MAX_WORKERS", d.max_workers)),
            max_upstream_concurrency=int(_num("MAX_UPSTREAM_CONCURRENCY", d.max_upstream_concurrency)),
            rate_limit_enabled=_bool("RATE_LIMIT_ENABLED", d.rate_limit_enabled),
            rate_limit_units=int(_num("RATE_LIMIT_UNITS", d.rate_limit_units)),
            rate_limit_window_s=int(_num("RATE_LIMIT_WINDOW_S", d.rate_limit_window_s)),
            trust_proxy_headers=_bool("TRUST_PROXY_HEADERS", d.trust_proxy_headers),
            rate_limit_exempt_ips=_tuple("RATE_LIMIT_EXEMPT_IPS", ""),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()

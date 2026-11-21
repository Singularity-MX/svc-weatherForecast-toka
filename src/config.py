"""All the settings live in this file. Change a value, restart, done.

There is no .env on purpose. The only thing read from the environment is the
Open-Meteo key, because it's a secret and shouldn't end up in git.

Routes and tests read from here too, so if you set api_version to "v3" the
endpoints move to /api/v3/... and the tests follow.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class Settings:
    # name and versions
    service_name: str = "svc-weatherForecast-toka"
    service_version: str = "3.1.0"      # shows up in swagger and in every response
    api_version: str = "v2"             # goes in the url: /api/<api_version>/...

    # server
    host: str = "0.0.0.0"
    port: int = 5000                    # in docker, pick the host port with -p <host>:5000

    # providers
    open_meteo_api_key: str = field(default_factory=lambda: os.getenv("OPEN_METEO_API_KEY", ""))  # secret, env var only

    # short term (ensembles)
    ensemble_models: Tuple[str, ...] = ("ecmwf_ifs025", "ecmwf_aifs025", "google_weathernext2")
    short_term_days: int = 15
    rain_threshold_mm: float = 1.0      # a member counts as "rain" at or above this
    heavy_rain_mm: float = 20.0         # generic numbers. per-crop limits are the recommender's job
    frost_c: float = 0.0                # frost if tmin <= this
    heat_c: float = 35.0                # heat if tmax >= this

    # seasonal
    seasonal_weekly_days: int = 46      # EC46 gives ~6 weeks
    seasonal_monthly_days: int = 210    # SEAS5, about 7 months
    temp_normal_band_c: float = 0.5     # inside +/- this band counts as "normal"
    precip_normal_band_pct: float = 20.0
    precip_min_climatology: float = 0.5  # if the model climatology is under this the % is meaningless (dry season), so we skip it
    seasonal_confidence: str = "low"

    # cache and network
    ttl_short_term_s: int = 3600
    ttl_seasonal_s: int = 6 * 3600
    cache_decimals: int = 2             # coords rounded to 0.01 deg (~1 km), that is what we call a cell
    http_timeout_s: float = 30.0
    max_workers: int = 4                # cells computed at the same time
    max_upstream_concurrency: int = 8   # calls to the providers at once

    # rate limit
    rate_limit_enabled: bool = True
    rate_limit_units: int = 120         # per client per window. 1 unit = 1 distinct cell
    rate_limit_window_s: int = 60
    trust_proxy_headers: bool = False   # only turn on behind a reverse proxy you trust
    rate_limit_exempt_ips: Tuple[str, ...] = ()   # e.g. ("10.0.0.5",) for the unifier

    def __post_init__(self) -> None:
        if not re.fullmatch(r"v\d+", self.api_version):
            raise ValueError(f"api_version must look like 'v1', 'v2'... (got {self.api_version!r})")
        if not 1 <= self.port <= 65535:
            raise ValueError(f"port out of range: {self.port}")

    # derived, don't edit
    @property
    def api_prefix(self) -> str:
        return f"/api/{self.api_version}"

    @property
    def ensemble_url(self) -> str:
        host = "customer-ensemble-api" if self.open_meteo_api_key else "ensemble-api"
        return f"https://{host}.open-meteo.com/v1/ensemble"

    @property
    def seasonal_url(self) -> str:
        host = "customer-seasonal-api" if self.open_meteo_api_key else "seasonal-api"
        return f"https://{host}.open-meteo.com/v1/seasonal"


settings = Settings()


def get_settings() -> Settings:
    return settings

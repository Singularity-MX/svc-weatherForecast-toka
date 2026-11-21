"""Short term forecast (up to 15 days) from the Open-Meteo ensemble API.

We pool the members of every model into one big ensemble. The "value" is the median,
the band is p10-p90 and a probability is just the share of members that cross a threshold.
"""
from __future__ import annotations

import logging
import re
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from .. import http_client
from ..config import get_settings
from ..http_client import UpstreamError
from ..schemas import (Band, DayExtremes, EnsembleModel, ExtremeThresholds, Metadata, PrecipBand, ShortTermDay,
                       ShortTermHorizon, ShortTermSummary)

log = logging.getLogger("toka.short_term")

# open-meteo has renamed models before, so each one has a list of names to try in order.
# (the "_ensemble" ones are what worked on the real response)
TOKENS: Dict[str, List[str]] = {
    "ecmwf_ifs025": ["ecmwf_ifs025_ensemble", "ecmwf_ifs025"],
    "ecmwf_aifs025": ["ecmwf_aifs025_ensemble", "ecmwf_aifs025"],
    "google_weathernext2": ["google_weathernext2_ensemble"],
}
DISPLAY_NAMES = {
    "ecmwf_ifs025": "ECMWF IFS",
    "ecmwf_aifs025": "ECMWF AIFS",
    "google_weathernext2": "Google WeatherNext 2",
}
_WORKING_TOKEN: Dict[str, str] = {}

CORE_VARS = ["temperature_2m_max", "temperature_2m_min", "temperature_2m_mean", "precipitation_sum"]
EXTRA_VARS = ["relative_humidity_2m_mean", "et0_fao_evapotranspiration", "shortwave_radiation_sum"]


@dataclass
class _ModelResult:
    id: str
    token: str
    dates: List[str]
    members: Dict[str, np.ndarray]   # variable -> array of (members, days)
    elevation: Optional[float] = None
    timezone: Optional[str] = None


def _extract_members(daily: dict, var: str, token: str) -> Optional[np.ndarray]:
    pattern = re.compile(rf"^{re.escape(var)}(?:_member\d+)?(?:_{re.escape(token)})?$")
    cols = [k for k in daily if pattern.match(k)]
    if not cols:
        return None
    return np.array([daily[c] for c in sorted(cols)], dtype=float)  # missing values come as None, this makes them nan


def _request(lat: float, lon: float, token: str, days: int, variables: List[str]) -> dict:
    s = get_settings()
    params = {
        "latitude": lat, "longitude": lon, "models": token, "forecast_days": days,
        "timezone": "auto", "daily": ",".join(variables),
    }
    if s.open_meteo_api_key:
        params["apikey"] = s.open_meteo_api_key
    return http_client.get_json(s.ensemble_url, params, "open-meteo-ensemble")


def _fetch_model(lat: float, lon: float, model_id: str, days: int) -> _ModelResult:
    candidates = TOKENS.get(model_id, [model_id])
    if model_id in _WORKING_TOKEN:
        candidates = [_WORKING_TOKEN[model_id]]
    last: Optional[UpstreamError] = None
    for token in candidates:
        try:
            try:
                data = _request(lat, lon, token, days, CORE_VARS + EXTRA_VARS)
            except UpstreamError as e:
                if e.status != 400:
                    raise
                data = _request(lat, lon, token, days, CORE_VARS)  # this model doesn't have the extra variables, retry with the basic ones
        except UpstreamError as e:
            last = e
            if e.status == 400:
                continue  # name not accepted, try the next one
            raise
        daily = data.get("daily") or {}
        if "time" not in daily:
            raise UpstreamError("open-meteo-ensemble", None, f"{model_id}: response has no 'daily' block")
        members = {v: m for v in CORE_VARS + EXTRA_VARS if (m := _extract_members(daily, v, token)) is not None}
        if "temperature_2m_mean" not in members or "precipitation_sum" not in members:
            raise UpstreamError("open-meteo-ensemble", None, f"{model_id}: temperature or precipitation missing")
        _WORKING_TOKEN[model_id] = token
        return _ModelResult(model_id, token, [t[:10] for t in daily["time"]], members,
                            data.get("elevation"), data.get("timezone"))
    raise last or UpstreamError("open-meteo-ensemble", None, f"{model_id}: no valid model token")


def _f(x: Optional[float], nd: int = 2) -> Optional[float]:
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def _percentiles(a: np.ndarray) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanpercentile(a, [10, 50, 90], axis=0)


def _band(a: Optional[np.ndarray], i: int, nd: int = 2) -> Optional[Band]:
    if a is None:
        return None
    p = _percentiles(a)
    if not np.isfinite(p[:, i]).any():
        return None
    return Band(p10=_f(p[0, i], nd), p50=_f(p[1, i], nd), p90=_f(p[2, i], nd))


def _prob(col: np.ndarray, cond) -> Optional[float]:
    valid = col[np.isfinite(col)]
    return _f(float(cond(valid).mean() * 100), 1) if valid.size else None


def _summary(precip: np.ndarray, threshold: float) -> Optional[ShortTermSummary]:
    # total rain and number of rainy days per member; members with gaps are left out
    complete = np.isfinite(precip).all(axis=1)
    if not complete.any():
        return None
    series = precip[complete]
    totals, rain_days = series.sum(axis=1), (series >= threshold).sum(axis=1).astype(float)

    def band(a: np.ndarray, nd: int) -> Band:
        p = np.percentile(a, [10, 50, 90])
        return Band(p10=_f(p[0], nd), p50=_f(p[1], nd), p90=_f(p[2], nd))
    return ShortTermSummary(precipitation_total_mm=band(totals, 1), rain_days=band(rain_days, 0))


def calculate(lat: float, lon: float) -> Tuple[ShortTermHorizon, Optional[float], Optional[str]]:
    s = get_settings()
    ids = list(s.ensemble_models)
    warnings_out: List[str] = []
    ok: List[_ModelResult] = []

    with ThreadPoolExecutor(max_workers=len(ids)) as ex:
        futures = {i: ex.submit(_fetch_model, lat, lon, i, s.short_term_days) for i in ids}
    for model_id, fut in futures.items():
        try:
            ok.append(fut.result())
        except UpstreamError as e:
            log.warning("model %s failed: %s", model_id, e)
            warnings_out.append(f"{DISPLAY_NAMES.get(model_id, model_id)} unavailable: {e.detail}")
    if not ok:
        raise UpstreamError("open-meteo-ensemble", None, "no ensemble model answered: " + "; ".join(warnings_out))

    dates = sorted({d for m in ok for d in m.dates})[: s.short_term_days]
    idx = {d: i for i, d in enumerate(dates)}

    def pool(var: str) -> Optional[np.ndarray]:
        blocks = []
        for m in ok:
            a = m.members.get(var)
            if a is None:
                continue
            full = np.full((a.shape[0], len(dates)), np.nan)
            for j, d in enumerate(m.dates):
                if d in idx and j < a.shape[1]:
                    full[:, idx[d]] = a[:, j]
            blocks.append(full)
        return np.vstack(blocks) if blocks else None

    tmax, tmin, tmean = pool("temperature_2m_max"), pool("temperature_2m_min"), pool("temperature_2m_mean")
    precip = pool("precipitation_sum")
    humidity, et0, radiation = pool("relative_humidity_2m_mean"), pool("et0_fao_evapotranspiration"), pool("shortwave_radiation_sum")
    precip_p = _percentiles(precip)

    days: List[ShortTermDay] = []
    for i, date in enumerate(dates):
        col = precip[:, i]
        valid = np.isfinite(col)
        n = int(valid.sum())
        prob = float((col[valid] >= s.rain_threshold_mm).mean() * 100) if n else None
        mean = float(np.nanmean(col)) if n else float("nan")
        extremes = DayExtremes(
            heavy_rain_probability_pct=_prob(col, lambda x: x >= s.heavy_rain_mm),
            frost_probability_pct=_prob(tmin[:, i] if tmin is not None else np.array([]), lambda x: x <= s.frost_c),
            heat_probability_pct=_prob(tmax[:, i] if tmax is not None else np.array([]), lambda x: x >= s.heat_c))
        days.append(ShortTermDay(
            date=date, day=i,
            temp_mean_c=_band(tmean, i) or Band(),
            temp_min_c=_band(tmin, i) or Band(),
            temp_max_c=_band(tmax, i) or Band(),
            precipitation_mm=PrecipBand(p10=_f(precip_p[0, i]), p50=_f(precip_p[1, i]), p90=_f(precip_p[2, i]), mean=_f(mean)),
            rain_probability_pct=_f(prob, 1),
            extremes=extremes,
            relative_humidity_pct=_band(humidity, i, 1), et0_mm=_band(et0, i), radiation_mj_m2=_band(radiation, i),
            members=n,
        ))

    block = ShortTermHorizon(
        source=" + ".join(DISPLAY_NAMES.get(m.id, m.id) for m in ok) + " ensembles (via Open-Meteo Ensemble API)",
        horizon_days=len(days), rain_threshold_mm=s.rain_threshold_mm,
        models=[EnsembleModel(id=m.id, token=m.token, members=int(m.members["temperature_2m_mean"].shape[0])) for m in ok],
        days=days, warnings=warnings_out, generated_utc=datetime.now(timezone.utc).isoformat(),
        extreme_thresholds=ExtremeThresholds(heavy_rain_mm=s.heavy_rain_mm, frost_c=s.frost_c, heat_c=s.heat_c),
        summary=_summary(precip, s.rain_threshold_mm),
        valid_from=dates[0], valid_to=dates[-1],
        metadata=Metadata(
            provider="Open-Meteo Ensemble API (" + ", ".join(m.token for m in ok) + ")",
            spatial_resolution="Native ~0.25 deg (~25 km) model grid; values are adjusted to the cell's elevation with a 90 m DEM "
                               "(this does not add real resolution)",
            temporal_resolution="daily aggregates of 1-6 hourly model steps",
            units={"temperature": "degC", "precipitation": "mm/day", "relative_humidity": "%", "et0": "mm/day",
                   "radiation": "MJ/m2/day", "probabilities": "% of ensemble members"},
            quality="Raw multi-model ensemble output; no local bias correction. Band = P10-P90 across pooled members."),
    )
    ref = next((m for m in ok if m.elevation is not None), ok[0])
    return block, ref.elevation, ref.timezone

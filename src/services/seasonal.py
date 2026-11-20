"""Horizon 2 - Seasonal: outlooks (not forecasts) from EC46 (weekly) and SEAS5 (monthly).

Open-Meteo computes anomalies against the model's own climatology (ECMWF hindcasts):
anomaly = forecast - model climatology. No external normal (NASA POWER) is subtracted.
"""
from __future__ import annotations

import calendar
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import numpy as np

from .. import http_client
from ..config import get_settings
from ..http_client import UpstreamError
from ..schemas import (Metadata, PrecipitationOutlook, PrecipProbabilities, SeasonalHorizon, SeasonalPeriod,
                       SeasonalThresholds, TempProbabilities, TemperatureOutlook)

log = logging.getLogger("toka.seasonal")

VARS = ["temperature_2m_mean", "temperature_2m_anomaly", "precipitation_mean", "precipitation_anomaly"]
PRECIP_EPS = 0.05  # model climatology below this (units/day): the ratio is unreliable


def _series(block: dict, var: str, model: str) -> Optional[np.ndarray]:
    pattern = re.compile(rf"^{re.escape(var)}(?:_member\d+)?(?:_{re.escape(model)})?$")
    cols = [k for k in block if pattern.match(k)]
    return np.array([block[c] for c in sorted(cols)], dtype=float) if cols else None


def _parse_date(s: str) -> date:
    s = s[:10]
    return date.fromisoformat(s + "-01" if len(s) == 7 else s)


def _round_to_100(fractions: List[float]) -> List[int]:
    raw = [f * 100 for f in fractions]
    base = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)[: 100 - sum(base)]:
        base[i] += 1
    return base


def _median(a: np.ndarray) -> Optional[float]:
    v = a[np.isfinite(a)]
    return round(float(np.median(v)), 2) if v.size else None


def _strength(probs: List[int]) -> str:
    top = max(probs)
    return "strong" if top >= 60 else ("moderate" if top >= 45 else "weak")


def _request(lat: float, lon: float, scale: str, model: str, days: int) -> dict:
    s = get_settings()
    params = {"latitude": lat, "longitude": lon, "models": model, "forecast_days": days,
              ("weekly" if scale == "weekly" else "monthly"): ",".join(VARS)}
    if s.open_meteo_api_key:
        params["apikey"] = s.open_meteo_api_key
    data = http_client.get_json(s.seasonal_url, params, "open-meteo-seasonal")
    block = data.get(scale)
    if not block or "time" not in block:
        raise UpstreamError("open-meteo-seasonal", None, f"response has no {scale} block")
    return block


def _periods(block: dict, scale: str, model: str) -> List[SeasonalPeriod]:
    s = get_settings()
    t_anom, p_anom = _series(block, "temperature_2m_anomaly", model), _series(block, "precipitation_anomaly", model)
    p_mean = _series(block, "precipitation_mean", model)
    if t_anom is None and p_anom is None:
        raise UpstreamError("open-meteo-seasonal", None, f"{model}: no anomaly variables")
    out: List[SeasonalPeriod] = []
    yesterday = date.today() - timedelta(days=1)

    for i, t in enumerate(block["time"]):
        start = _parse_date(t)
        end = start + timedelta(days=6) if scale == "weekly" else start.replace(day=calendar.monthrange(start.year, start.month)[1])
        if end < yesterday:
            continue
        n = 0
        all_probs: List[int] = []

        temp = TemperatureOutlook()
        if t_anom is not None:
            v = t_anom[:, i][np.isfinite(t_anom[:, i])]
            if v.size:
                n = max(n, v.size)
                cold, warm = float((v < -s.temp_normal_band_c).mean()), float((v > s.temp_normal_band_c).mean())
                p = _round_to_100([cold, 1 - cold - warm, warm])
                all_probs += p
                temp = TemperatureOutlook(
                    anomaly_c=_median(v), probabilities_pct=TempProbabilities(colder=p[0], normal=p[1], warmer=p[2]),
                    most_likely_category=("colder", "normal", "warmer")[int(np.argmax(p))])

        precip = PrecipitationOutlook()
        if p_anom is not None and p_mean is not None:
            an, me = p_anom[:, i], p_mean[:, i]
            clim = float(np.nanmean(me - an)) if np.isfinite(me - an).any() else float("nan")
            if np.isfinite(clim) and clim >= PRECIP_EPS:
                pct = an / clim * 100
                v = pct[np.isfinite(pct)]
                n = max(n, v.size)
                dry, wet = float((v < -s.precip_normal_band_pct).mean()), float((v > s.precip_normal_band_pct).mean())
                p = _round_to_100([dry, 1 - dry - wet, wet])
                all_probs += p
                precip = PrecipitationOutlook(
                    anomaly_pct=_median(v), probabilities_pct=PrecipProbabilities(drier=p[0], normal=p[1], wetter=p[2]),
                    most_likely_category=("drier", "normal", "wetter")[int(np.argmax(p))])
            else:
                precip = PrecipitationOutlook(note="Model climatology is ~0 mm: inconclusive signal.")
        if not all_probs:
            continue
        out.append(SeasonalPeriod(
            start=start.isoformat(), end=end.isoformat(), scale=scale, model="EC46" if scale == "weekly" else "SEAS5",
            members=int(n), temperature=temp, precipitation=precip, signal_strength=_strength(all_probs)))
    return out


def calculate(lat: float, lon: float) -> SeasonalHorizon:
    s = get_settings()
    plan = (("weekly", "ecmwf_ec46", s.seasonal_weekly_days), ("monthly", "ecmwf_seas5", s.seasonal_monthly_days))

    def task(item):
        scale, model, days = item
        return _periods(_request(lat, lon, scale, model, days), scale, model)

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(task, item) for item in plan]
    results, warnings_out = {}, []
    for item, fut in zip(plan, futures):
        try:
            results[item[0]] = fut.result()
        except UpstreamError as e:
            log.warning("seasonal %s failed: %s", item[0], e)
            warnings_out.append(f"{item[0]} scale unavailable: {e.detail}")
    if not results:
        raise UpstreamError("open-meteo-seasonal", None, "; ".join(warnings_out))

    periods = results.get("weekly", []) + results.get("monthly", [])
    return SeasonalHorizon(
        valid_from=min((p.start for p in periods), default=None), valid_to=max((p.end for p in periods), default=None),
        metadata=Metadata(
            provider="Open-Meteo Seasonal API (ECMWF EC46 + SEAS5)",
            spatial_resolution="Native ~36 km model grid",
            temporal_resolution="weekly (EC46, up to 6 weeks) and monthly (SEAS5, up to 7 months)",
            units={"temperature_anomaly": "degC", "precipitation_anomaly": "% of the model climatology",
                   "probabilities": "% of ensemble members"},
            quality="No bias correction. Anomalies are relative to the ECMWF hindcast climatology. Skill is limited over central Mexico."),
        confidence=s.seasonal_confidence,
        thresholds=SeasonalThresholds(temp_normal_band_c=s.temp_normal_band_c, precip_normal_band_pct=s.precip_normal_band_pct),
        weekly=results.get("weekly", []), monthly=results.get("monthly", []), warnings=warnings_out,
        generated_utc=datetime.now(timezone.utc).isoformat())

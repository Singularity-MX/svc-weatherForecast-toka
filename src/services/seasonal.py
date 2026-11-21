"""Seasonal outlook: weekly from EC46, monthly from SEAS5.

Open-Meteo already gives these averaged over the ensemble (one series, no members; we saw
it on a real response), so all we can do is report the anomaly and a tendency. Per-member
probabilities are not possible here.

The anomaly is against the ECMWF model's own climatology, not against NASA or the
historical service, so don't mix them.
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
from ..schemas import Metadata, PrecipitationOutlook, SeasonalHorizon, SeasonalPeriod, SeasonalThresholds, TemperatureOutlook

log = logging.getLogger("toka.seasonal")

VARS = ["temperature_2m_mean", "temperature_2m_anomaly", "precipitation_mean", "precipitation_anomaly"]


def _series(block: dict, var: str, model: str) -> Optional[np.ndarray]:
    pattern = re.compile(rf"^{re.escape(var)}(?:_member\d+)?(?:_{re.escape(model)})?$")
    cols = [k for k in block if pattern.match(k)]
    return np.array([block[c] for c in sorted(cols)], dtype=float) if cols else None


def _parse_date(s: str) -> date:
    s = s[:10]
    return date.fromisoformat(s + "-01" if len(s) == 7 else s)


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


def _mean(a: np.ndarray) -> Optional[float]:
    v = a[np.isfinite(a)]
    return float(v.mean()) if v.size else None


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

        temp = TemperatureOutlook()
        if t_anom is not None and (an := _mean(t_anom[:, i])) is not None:
            tendency = "warmer" if an > s.temp_normal_band_c else ("colder" if an < -s.temp_normal_band_c else "normal")
            temp = TemperatureOutlook(anomaly_c=round(an, 2), tendency=tendency)

        precip = PrecipitationOutlook()
        if p_anom is not None and p_mean is not None:
            an, me = _mean(p_anom[:, i]), _mean(p_mean[:, i])
            if an is not None and me is not None:
                clim = me - an  # the climatology the model used = value minus anomaly
                if clim >= s.precip_min_climatology:
                    pct = an / clim * 100
                    tendency = "wetter" if pct > s.precip_normal_band_pct else ("drier" if pct < -s.precip_normal_band_pct else "normal")
                    precip = PrecipitationOutlook(anomaly_pct=round(pct, 1), tendency=tendency)
                else:
                    precip = PrecipitationOutlook(note="Model climatology is too small (dry season): a relative anomaly is not meaningful.")
        if temp.anomaly_c is None and precip.anomaly_pct is None and precip.note is None:
            continue
        out.append(SeasonalPeriod(start=start.isoformat(), end=end.isoformat(), scale=scale,
                                  model="EC46" if scale == "weekly" else "SEAS5", temperature=temp, precipitation=precip))
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
            units={"temperature_anomaly": "degC", "precipitation_anomaly": "% of the model climatology"},
            quality="Ensemble-mean values (no spread or probabilities). No bias correction. Anomalies are relative to the ECMWF "
                    "hindcast climatology. Skill is limited over central Mexico."),
        confidence=s.seasonal_confidence,
        thresholds=SeasonalThresholds(temp_normal_band_c=s.temp_normal_band_c, precip_normal_band_pct=s.precip_normal_band_pct,
                                       precip_min_climatology=s.precip_min_climatology),
        weekly=results.get("weekly", []), monthly=results.get("monthly", []), warnings=warnings_out,
        generated_utc=datetime.now(timezone.utc).isoformat())

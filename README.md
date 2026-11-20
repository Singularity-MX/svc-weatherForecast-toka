<img src="./img/img.png" alt="Singularity Logo" width="100%"/>

# Toka Weather Forecast Service

In the Toka architecture this is the **future-climate service**: the environmental data unifier calls it to build a
field's environmental context. It delivers evidence with metadata and uncertainty; it holds **no agronomic logic**.
Historical climate (normals, trends) is served by `svc-weatherHistory-toka`, not here.

| Horizon | `type` | Source | Delivers |
|---|---|---|---|
| `short_term` | `forecast` | ECMWF IFS/AIFS + Google WeatherNext 2 ensembles (Open-Meteo Ensemble API), up to 15 days | median, P10-P90, rain probability, frost / heat / heavy-rain probabilities, accumulated rain |
| `seasonal` | `outlook` | ECMWF EC46 (weekly) + SEAS5 (monthly), Open-Meteo Seasonal API | category probabilities, **low confidence** |

A multi-day weather forecast is not a seasonal projection; the two are never merged. No NASA dataset is consumed by this service.

## Run
```bash
pip install -r requirements-runtime.txt
uvicorn src.main:app --host 0.0.0.0 --port 5000
```
Swagger `/docs` - ReDoc `/redoc` - OpenAPI `/openapi.json`

### Docker (the port is configurable)
```bash
docker build -t svc-weatherforecast-toka .                       # default port 5000
docker build --build-arg PORT=8080 -t svc-weatherforecast-toka . # bake another port
docker run -d -p 5000:5000 svc-weatherforecast-toka
docker run -d -e PORT=8080 -p 8080:8080 svc-weatherforecast-toka # override at run time
```

## API
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/forecast` | Field polygon (1-50 vertices; use 1 for a point). Rate limited |
| GET | `/health` | Liveness. Not rate limited |

```bash
curl -X POST localhost:5000/api/v1/forecast -H 'Content-Type: application/json' \
  -d '{"polygon":[{"lat":20.9215,"lon":-101.423},{"lat":20.923,"lon":-101.421}],"horizons":["short_term","seasonal"]}'
```
**Cells:** vertices are grouped into ~1 km cells (`CACHE_DECIMALS`=2). A parcel normally yields one cell, so the response is
not repeated per vertex: `cells[].vertices` lists the 1-based request vertices each result covers.

```jsonc
{ "service": "svc-weatherForecast-toka", "version": "3.0.0", "timestamp_utc": "...",
  "total_vertices": 4, "total_cells": 1, "requested_horizons": ["short_term", "seasonal"],
  "cells": [{
    "vertices": [1, 2, 3, 4],
    "location": { "latitude": 20.92, "longitude": -101.42, "elevation_m": 1767.0, "timezone": "America/Mexico_City" },
    "short_term": { "type": "forecast", "available": true, "valid_from": "...", "valid_to": "...", "generated_utc": "...",
                    "metadata": { "provider": "...", "crs": "EPSG:4326", "spatial_resolution": "...", "units": {}, "quality": "..." },
                    "summary": { "precipitation_total_mm": {}, "rain_days": {} },
                    "days": [{ "date": "...", "temp_mean_c": {"p10": 0, "p50": 0, "p90": 0}, "rain_probability_pct": 0,
                               "extremes": { "heavy_rain_probability_pct": 0, "frost_probability_pct": 0, "heat_probability_pct": 0 } }] },
    "seasonal": { "type": "outlook", "confidence": "low", "weekly": [], "monthly": [] } }] }
```
Every horizon has `available`/`error`/`warnings`: a failed horizon does not hide the other; `502` only when all fail.
Extreme thresholds (`HEAVY_RAIN_MM`=20, `FROST_C`=0, `HEAT_C`=35) are generic and returned in `extreme_thresholds`;
crop-specific tolerances belong to the recommendation system.

## Rate limiting
Per client IP, sliding window, in memory: `RATE_LIMIT_UNITS` (120) per `RATE_LIMIT_WINDOW_S` (60 s); 1 unit = 1 distinct cell.
Headers `X-RateLimit-Limit/-Remaining/-Reset/-Cost`; over budget -> `429` + `Retry-After`.
Behind a trusted proxy set `TRUST_PROXY_HEADERS=true`. Callers inside Toka (the unifier) share one IP: put it in
`RATE_LIMIT_EXEMPT_IPS` or raise the budget. State is per process (no shared store).

## Environment variables
`PORT` - `OPEN_METEO_API_KEY` (commercial use) - `ENSEMBLE_MODELS` - `SHORT_TERM_DAYS` - `RAIN_THRESHOLD_MM` - `HEAVY_RAIN_MM` - `FROST_C` -
`HEAT_C` - `TEMP_NORMAL_BAND_C` - `PRECIP_NORMAL_BAND_PCT` - `SEASONAL_CONFIDENCE` - `TTL_SHORT_TERM_S` - `TTL_SEASONAL_S` - `HTTP_TIMEOUT_S` -
`MAX_WORKERS` - `MAX_UPSTREAM_CONCURRENCY` - `RATE_LIMIT_ENABLED` - `RATE_LIMIT_UNITS` - `RATE_LIMIT_WINDOW_S` - `TRUST_PROXY_HEADERS` -
`RATE_LIMIT_EXEMPT_IPS`

## Layout
```
src/main.py  config.py  schemas.py  rate_limit.py  cache.py  http_client.py
src/services/  short_term.py  seasonal.py  forecast.py (orchestrator, groups vertices into cells)
src/routers/   forecast.py  health.py
tests/test_api.py   (simulated providers)
```


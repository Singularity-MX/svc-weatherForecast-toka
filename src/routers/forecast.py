from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from .. import rate_limit
from ..config import SERVICE_NAME, VERSION
from ..schemas import ErrorResponse, PolygonForecastResponse, PolygonRequest
from ..services import forecast as forecast_service

router = APIRouter(prefix="/api/v1", tags=["Forecast"])

_RATE_HEADERS = {
    "X-RateLimit-Limit": {"description": "Units allowed per window", "schema": {"type": "integer"}},
    "X-RateLimit-Remaining": {"description": "Units left in the current window", "schema": {"type": "integer"}},
    "X-RateLimit-Reset": {"description": "Seconds until the whole budget is restored", "schema": {"type": "integer"}},
    "X-RateLimit-Cost": {"description": "Units charged to this request", "schema": {"type": "integer"}},
}

DESCRIPTION = """
Future-climate service of Toka. Takes a field polygon (1-50 vertices) and returns the requested horizons
for each distinct ~1 km **cell** (vertices of one parcel normally share a single cell, so the payload is not repeated).

| Horizon | `type` | What it is |
|---|---|---|
| `short_term` | `forecast` | 1-15 days from multi-model ensembles: median, P10-P90 band, rain probability, frost / heat / heavy-rain probabilities, accumulated rain |
| `seasonal` | `outlook` | Weeks (EC46) and months (SEAS5) as category probabilities. **Low confidence**; not a forecast |

A weather forecast of days is **not** a seasonal climate projection: keep the two separate in the UI.
Historical normals are provided by `svc-weatherHistory-toka`, not by this service.

Every horizon carries `metadata` (provider, CRS, resolution, units, quality), `valid_from` / `valid_to` and `generated_utc`.
If a horizon fails it comes back with `available: false` and an `error`; `502` only when **all** requested horizons fail.

**Rate limit:** 1 unit = 1 distinct cell in the request (a parcel normally costs 1). Over budget -> `429` + `Retry-After`.
"""


@router.post(
    "/forecast", response_model=PolygonForecastResponse, summary="Forecast for a field polygon",
    description=DESCRIPTION, operation_id="forecast_polygon",
    responses={
        200: {"headers": _RATE_HEADERS},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded. See the `Retry-After` header (seconds).",
              "headers": {"Retry-After": {"description": "Seconds to wait before retrying", "schema": {"type": "integer"}}}},
        502: {"model": ErrorResponse, "description": "Every provider failed for all requested horizons."},
    })
def post_forecast(payload: PolygonRequest, request: Request, response: Response) -> PolygonForecastResponse:
    points = [(c.lat, c.lon) for c in payload.polygon]
    rate_limit.enforce(request, response, cost=rate_limit.distinct_cells(points))
    cells = forecast_service.compute_polygon(points, payload.horizons)
    return PolygonForecastResponse(
        service=SERVICE_NAME, version=VERSION, timestamp_utc=datetime.now(timezone.utc).isoformat(),
        total_vertices=len(points), total_cells=len(cells), requested_horizons=payload.horizons, cells=cells)

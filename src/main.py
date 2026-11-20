import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from .config import SERVICE_NAME, VERSION
from .http_client import UpstreamError
from .routers import forecast, health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

DESCRIPTION = """
**Future-climate service** of *Toka*: consumed by the environmental data unifier to build a field's environmental context.
Each horizon declares what it is, so the UI and the recommendation system never confuse them.

1. **Short term (1-15 days)** - `forecast`: ECMWF IFS/AIFS and Google WeatherNext 2 ensembles (Open-Meteo): median, P10-P90 band, rain probability, extremes.
2. **Seasonal (weeks to 7 months)** - `outlook`: ECMWF EC46 + SEAS5 category probabilities. **Low confidence** over central Mexico.

Historical climate (normals, trends) is **not** served here: it belongs to `svc-weatherHistory-toka`.
No agronomic logic lives in this service; it only delivers environmental evidence with its metadata and uncertainty.

> Open-Meteo is free for **non-commercial** use only; set `OPEN_METEO_API_KEY` for commercial use.
> Forecast endpoints are **rate limited** per client (see `X-RateLimit-*` headers and `429` responses).
"""

TAGS = [
    {"name": "Forecast", "description": "Field polygon forecast. Rate limited."},
    {"name": "Health", "description": "Availability probes. Not rate limited."},
]


def create_app() -> FastAPI:
    app = FastAPI(
        title=SERVICE_NAME,
        description=DESCRIPTION,
        version=VERSION,
        openapi_tags=TAGS,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS: Local development only, solo localhost 
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(forecast.router)

    @app.exception_handler(UpstreamError)
    async def _upstream_error(_: Request, exc: UpstreamError):
        return JSONResponse(
            status_code=502,
            content={"detail": f"Weather provider error: {exc}"}
        )

    @app.get("/", include_in_schema=False)
    def _root():
        return RedirectResponse("/docs")

    return app


app = create_app()
"""Request and response models. Swagger (/docs) is generated from these."""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Horizon = Literal["short_term", "seasonal"]
ALL_HORIZONS: List[str] = ["short_term", "seasonal"]


# request
class Coordinate(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0, description="Vertex latitude (decimal degrees, WGS84)", examples=[20.9215])
    lon: float = Field(..., ge=-180.0, le=180.0, description="Vertex longitude (decimal degrees, WGS84)", examples=[-101.423])


class PolygonRequest(BaseModel):
    polygon: List[Coordinate] = Field(
        ..., min_length=1, max_length=50,
        description="Field (parcel) vertices, 1 to 50. Use a single vertex for a point query.",
    )
    horizons: List[Horizon] = Field(
        default_factory=lambda: list(ALL_HORIZONS), min_length=1,
        description="Horizons to compute. Defaults to both.",
    )

    @field_validator("horizons")
    @classmethod
    def _dedupe(cls, v: List[str]) -> List[str]:
        return list(dict.fromkeys(v))

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "polygon": [
            {"lat": 20.9215, "lon": -101.4230},
            {"lat": 20.9230, "lon": -101.4210},
            {"lat": 20.9205, "lon": -101.4195},
            {"lat": 20.9195, "lon": -101.4220},
        ],
        "horizons": ["short_term", "seasonal"],
    }]})


# shared
class Metadata(BaseModel):
    """Where the data comes from and how good it is (units, resolution, CRS, quality)."""
    provider: str
    crs: str = "EPSG:4326"
    spatial_resolution: str
    temporal_resolution: str
    units: Dict[str, str]
    quality: str


class _HorizonBase(BaseModel):
    available: bool = Field(True, description="False when the provider failed; see `error`.")
    error: Optional[str] = Field(None, description="Failure reason when `available` is false.")
    warnings: List[str] = Field(default_factory=list, description="Partial degradations (e.g. one ensemble model did not answer).")
    generated_utc: Optional[str] = Field(None, description="When the provider was queried (UTC). Open-Meteo does not expose the model run's initialization time.")
    valid_from: Optional[str] = Field(None, description="First date covered (YYYY-MM-DD)")
    valid_to: Optional[str] = Field(None, description="Last date covered (YYYY-MM-DD)")
    metadata: Optional[Metadata] = None


# short term
class Band(BaseModel):
    p10: Optional[float] = Field(None, description="10th percentile across ensemble members")
    p50: Optional[float] = Field(None, description="Median across members (central value)")
    p90: Optional[float] = Field(None, description="90th percentile across members")


class PrecipBand(Band):
    mean: Optional[float] = Field(None, description="Mean across members (mm)")


class EnsembleModel(BaseModel):
    id: str = Field(..., description="Logical model id")
    token: str = Field(..., description="Exact model name accepted by Open-Meteo")
    members: int = Field(..., description="Ensemble members contributed")


class DayExtremes(BaseModel):
    heavy_rain_probability_pct: Optional[float] = Field(None, description="% of members with daily precipitation >= heavy_rain_mm")
    frost_probability_pct: Optional[float] = Field(None, description="% of members with daily minimum <= frost_c")
    heat_probability_pct: Optional[float] = Field(None, description="% of members with daily maximum >= heat_c")


class ExtremeThresholds(BaseModel):
    heavy_rain_mm: float
    frost_c: float
    heat_c: float
    note: str = "Generic thresholds. Crop-specific tolerances belong to the recommendation system."


class ShortTermDay(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD (local time zone of the cell)")
    day: int = Field(..., description="0 = today")
    temp_mean_c: Band
    temp_min_c: Band
    temp_max_c: Band
    precipitation_mm: PrecipBand
    rain_probability_pct: Optional[float] = Field(None, description="% of members with daily precipitation >= rain_threshold_mm")
    extremes: DayExtremes
    relative_humidity_pct: Optional[Band] = Field(None, description="Daily mean relative humidity, when the model provides it")
    et0_mm: Optional[Band] = Field(None, description="FAO-56 reference evapotranspiration, when available")
    radiation_mj_m2: Optional[Band] = Field(None, description="Daily shortwave radiation sum, when available")
    members: int = Field(..., description="Members with valid data for that day")


class ShortTermSummary(BaseModel):
    precipitation_total_mm: Optional[Band] = Field(None, description="Accumulated precipitation over the whole horizon (per-member sum)")
    rain_days: Optional[Band] = Field(None, description="Number of days with precipitation >= rain_threshold_mm (per-member count)")


class ShortTermHorizon(_HorizonBase):
    type: Literal["forecast"] = "forecast"
    source: str = "ECMWF IFS/AIFS + Google WeatherNext 2 ensembles (via Open-Meteo Ensemble API)"
    horizon_days: int = 15
    uncertainty: str = "P10-P90"
    rain_threshold_mm: float = 1.0
    extreme_thresholds: Optional[ExtremeThresholds] = None
    models: List[EnsembleModel] = Field(default_factory=list)
    summary: Optional[ShortTermSummary] = None
    days: List[ShortTermDay] = Field(default_factory=list)


# seasonal
class TemperatureOutlook(BaseModel):
    anomaly_c: Optional[float] = Field(None, description="Ensemble-mean anomaly (deg C) vs. the ECMWF model's own climatology")
    tendency: Optional[Literal["colder", "normal", "warmer"]] = Field(None, description="Sign of the anomaly beyond +/- temp_normal_band_c. A tendency, NOT a probability.")


class PrecipitationOutlook(BaseModel):
    anomaly_pct: Optional[float] = Field(None, description="Ensemble-mean relative anomaly (%) vs. the model climatology. Null when that climatology is too small for a ratio to mean anything.")
    tendency: Optional[Literal["drier", "normal", "wetter"]] = Field(None, description="Sign of the anomaly beyond +/- precip_normal_band_pct. A tendency, NOT a probability.")
    note: Optional[str] = None


class SeasonalPeriod(BaseModel):
    start: str
    end: str
    scale: Literal["weekly", "monthly"]
    model: str = Field(..., description="EC46 (weekly) or SEAS5 (monthly)")
    temperature: TemperatureOutlook
    precipitation: PrecipitationOutlook


class SeasonalThresholds(BaseModel):
    temp_normal_band_c: float
    precip_normal_band_pct: float
    precip_min_climatology: float = Field(..., description="Model climatology (units/day) below which the precipitation ratio is withheld")
    criterion: str = "Fixed thresholds applied to the ensemble-mean anomaly (not hindcast terciles)."


class SeasonalHorizon(_HorizonBase):
    type: Literal["outlook"] = "outlook"
    source: str = "ECMWF EC46 (weekly) + SEAS5 (monthly), via Open-Meteo Seasonal API"
    format: str = "ensemble-mean anomaly and tendency"
    confidence: str = Field("low", description="Skill is limited over central Mexico; always show this in the UI.")
    note: str = ("Outlook, not a forecast. Open-Meteo returns weekly/monthly values already averaged over the ensemble, so no "
                 "member spread or category probabilities are available here. Anomalies are relative to the ECMWF model's own "
                 "climatology, not to the normals of svc-weatherHistory-toka, and carry no local bias correction.")
    thresholds: Optional[SeasonalThresholds] = None
    weekly: List[SeasonalPeriod] = Field(default_factory=list)
    monthly: List[SeasonalPeriod] = Field(default_factory=list)


# response
class CellLocation(BaseModel):
    latitude: float = Field(..., description="Latitude actually queried (rounded to the ~1 km cache cell)")
    longitude: float = Field(..., description="Longitude actually queried (rounded to the ~1 km cache cell)")
    elevation_m: Optional[float] = Field(None, description="Elevation (m a.s.l.) from Open-Meteo's 90 m DEM. Only when short_term was computed.")
    timezone: Optional[str] = Field(None, description="IANA time zone. Only when short_term was computed.")


class CellForecast(BaseModel):
    """Result for one ~1 km cell. Vertices that land in the same cell share it."""
    vertices: List[int] = Field(..., description="1-based indexes of the request vertices covered by this cell")
    location: CellLocation
    short_term: Optional[ShortTermHorizon] = None
    seasonal: Optional[SeasonalHorizon] = None


class PolygonForecastResponse(BaseModel):
    service: str = "svc-weatherForecast-toka"
    version: str
    timestamp_utc: str
    total_vertices: int
    total_cells: int
    requested_horizons: List[Horizon]
    cells: List[CellForecast]


class ErrorResponse(BaseModel):
    detail: str


class RateLimitInfo(BaseModel):
    enabled: bool
    units: int
    window_s: int


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str
    version: str
    api_version: str
    port: int
    ensemble_models: List[str]
    rate_limit: RateLimitInfo
    cache: dict

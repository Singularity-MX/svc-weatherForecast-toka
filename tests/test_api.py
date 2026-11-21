import datetime as dt

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src import http_client, rate_limit
from src.config import Settings, get_settings
from src.cache import SEASONAL_CACHE, SHORT_TERM_CACHE
from src.http_client import UpstreamError
from src.main import app
from src.rate_limit import SlidingWindowLimiter

rng = np.random.default_rng(0)
CALLS = []


def _members(base, n, k, var, sd=1.0):
    return {(var if m == 0 else f"{var}_member{m:02d}"): list(np.round(base + rng.normal(0, sd, k), 2)) for m in range(n)}


def fake_get_json(url, params, provider):
    CALLS.append((provider, params.get("models")))
    if provider == "open-meteo-ensemble":
        token = params["models"]
        if token == "ecmwf_aifs025_ensemble":
            raise UpstreamError(provider, 400, "Cannot initialize model")   # forces the "try the next model name" path
        if token == "google_weathernext2_ensemble":
            raise UpstreamError(provider, 502, "down")                       # one model down, the rest should still work
        k, t0 = 15, dt.date.today()
        d = {"time": [(t0 + dt.timedelta(days=i)).isoformat() for i in range(k)]}
        d.update(_members(25, 51, k, "temperature_2m_max"))
        d.update(_members(10, 51, k, "temperature_2m_min"))
        d.update(_members(17, 51, k, "temperature_2m_mean"))
        d.update({a: [max(0, x) for x in b] for a, b in _members(2, 51, k, "precipitation_sum", sd=3).items()})
        d.update(_members(50, 51, k, "relative_humidity_2m_mean", sd=5))
        return {"elevation": 1767.0, "timezone": "America/Mexico_City", "daily": d}
    if provider == "open-meteo-seasonal":
        today = dt.date.today()
        if "weekly" in params:
            times, key = [(today + dt.timedelta(days=7 * i)).isoformat() for i in range(6)], "weekly"
        else:
            times, key = [(today.replace(day=1) + dt.timedelta(days=32 * i)).replace(day=1).isoformat() for i in range(7)], "monthly"
        k = len(times)
        # this is what the real api returns: one series per variable, no members
        b = {"time": times,
             "temperature_2m_mean": [15.0] * k,
             "temperature_2m_anomaly": [1.2, 0.1, -0.9, 0.4, 0.6, 1.5, -0.2][:k],
             "precipitation_mean": [1.5, 0.1, 2.0, 1.0, 1.0, 0.2, 3.0][:k],      # climatology = mean - anomaly
             "precipitation_anomaly": [0.75, 0.05, -1.0, 0.0, 0.5, -0.1, 1.2][:k]}
        return {key: b}
    raise AssertionError(provider)


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(http_client, "get_json", fake_get_json)
    monkeypatch.setattr(rate_limit, "limiter", SlidingWindowLimiter(1000, 60))
    for c in (SHORT_TERM_CACHE, SEASONAL_CACHE):
        c.clear()
    CALLS.clear()


client = TestClient(app)
PREFIX = get_settings().api_prefix            # change api_version in config.py and the tests follow
FORECAST = f"{PREFIX}/forecast"
PARCEL = {"polygon": [{"lat": 20.9215, "lon": -101.423}, {"lat": 20.9230, "lon": -101.421},
                      {"lat": 20.9205, "lon": -101.4195}, {"lat": 20.9195, "lon": -101.422}]}


# swagger / openapi
def test_swagger_and_openapi():
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {"/health", FORECAST}          # only these two, nothing extra
    assert list(spec["paths"][FORECAST]) == ["post"]
    assert "429" in spec["paths"][FORECAST]["post"]["responses"]


# forecast
def test_parcel_collapses_into_one_cell_with_both_horizons():
    r = client.post(FORECAST, json=PARCEL)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["total_vertices"] == 4 and j["total_cells"] == 1 and j["requested_horizons"] == ["short_term", "seasonal"]
    cell = j["cells"][0]
    assert cell["vertices"] == [1, 2, 3, 4]
    assert cell["location"]["elevation_m"] == 1767.0 and cell["location"]["timezone"] == "America/Mexico_City"
    assert "climatology" not in cell

    st = cell["short_term"]
    assert st["available"] and st["type"] == "forecast" and st["horizon_days"] == 15 and len(st["days"]) == 15
    assert st["warnings"], "a failing WeatherNext must surface as a warning"
    assert st["valid_from"] == st["days"][0]["date"] and st["valid_to"] == st["days"][-1]["date"]
    d = st["days"][0]
    assert d["temp_mean_c"]["p10"] <= d["temp_mean_c"]["p50"] <= d["temp_mean_c"]["p90"]
    assert 0 <= d["rain_probability_pct"] <= 100 and d["relative_humidity_pct"]["p50"]
    for k in ("heavy_rain_probability_pct", "frost_probability_pct", "heat_probability_pct"):
        assert 0 <= d["extremes"][k] <= 100
    assert d["extremes"]["heat_probability_pct"] == 0.0 and d["extremes"]["frost_probability_pct"] == 0.0   # the fake data is 25/10 degC
    sm = st["summary"]
    assert sm["precipitation_total_mm"]["p10"] <= sm["precipitation_total_mm"]["p50"] <= sm["precipitation_total_mm"]["p90"]
    assert 0 <= sm["rain_days"]["p50"] <= 15
    assert st["extreme_thresholds"]["frost_c"] == 0.0

    se = cell["seasonal"]
    assert se["type"] == "outlook" and se["confidence"] == "low" and se["weekly"] and se["monthly"]
    assert "probabilit" not in str(se["weekly"]) and "members" not in str(se["monthly"])   # we must not invent probabilities
    w = se["weekly"][0]
    assert w["temperature"] == {"anomaly_c": 1.2, "tendency": "warmer"}
    assert w["precipitation"]["tendency"] == "wetter" and w["precipitation"]["anomaly_pct"] == 100.0   # 0.75 / (1.5-0.75)
    m = se["monthly"]
    assert m[2]["temperature"]["tendency"] == "colder"
    assert m[1]["precipitation"]["anomaly_pct"] is None and m[1]["precipitation"]["note"]   # clim is 0.05, below 0.5, so no %

    for h in (st, se):                                   # both horizons must carry metadata
        m = h["metadata"]
        assert m["crs"] == "EPSG:4326" and m["units"] and m["spatial_resolution"] and m["quality"]
        assert h["generated_utc"] and h["valid_from"] and h["valid_to"]


def test_distinct_cells_are_separate_results():
    far = {"polygon": [{"lat": 20.0, "lon": -101.0}, {"lat": 20.0001, "lon": -101.0001}, {"lat": 21.0, "lon": -101.0}]}
    j = client.post(FORECAST, json=far).json()
    assert j["total_vertices"] == 3 and j["total_cells"] == 2
    assert [c["vertices"] for c in j["cells"]] == [[1, 2], [3]]


def test_same_cell_is_fetched_once_and_cached():
    client.post(FORECAST, json=PARCEL)
    n = len(CALLS)
    assert len([c for c in CALLS if c[0] == "open-meteo-seasonal"]) == 2        # weekly and monthly, one call each
    client.post(FORECAST, json=PARCEL)
    assert len(CALLS) == n                                                      # second time comes from cache


def test_single_vertex_and_horizon_filter():
    body = {"polygon": [{"lat": 20.92, "lon": -101.42}], "horizons": ["seasonal"]}
    j = client.post(FORECAST, json=body).json()
    assert j["total_cells"] == 1 and j["cells"][0]["short_term"] is None and j["cells"][0]["seasonal"]["available"]
    assert not any(c[0] == "open-meteo-ensemble" for c in CALLS)


def test_validation_422():
    assert client.post(FORECAST, json={"polygon": []}).status_code == 422
    assert client.post(FORECAST, json={"poligono": [{"lat": 1, "lon": 1}]}).status_code == 422
    assert client.post(FORECAST, json={"polygon": [{"lat": 95, "lon": 0}]}).status_code == 422
    assert client.post(FORECAST, json={"polygon": [{"lat": 1, "lon": 1}], "horizons": ["climatology"]}).status_code == 422


def test_partial_and_total_failure(monkeypatch):
    def seasonal_down(url, params, provider):
        if provider == "open-meteo-seasonal":
            raise UpstreamError(provider, 503, "unavailable")
        return fake_get_json(url, params, provider)
    monkeypatch.setattr(http_client, "get_json", seasonal_down)
    c = client.post(FORECAST, json=PARCEL).json()["cells"][0]
    assert c["seasonal"]["available"] is False and c["seasonal"]["error"]
    assert c["short_term"]["available"] is True

    def all_down(url, params, provider):
        raise UpstreamError(provider, 503, "down")
    monkeypatch.setattr(http_client, "get_json", all_down)
    for cache in (SHORT_TERM_CACHE, SEASONAL_CACHE):
        cache.clear()
    assert client.post(FORECAST, json=PARCEL).status_code == 502


# rate limit
def test_limiter_sliding_window_unit():
    now = [0.0]
    lim = SlidingWindowLimiter(3, 60, clock=lambda: now[0])
    assert [lim.hit("a").allowed for _ in range(3)] == [True, True, True]
    d = lim.hit("a")
    assert not d.allowed and d.remaining == 0 and d.retry_after_s == pytest.approx(60)
    assert lim.hit("b").allowed                       # other clients are not affected
    now[0] = 61
    assert lim.hit("a").allowed                       # window moved on
    assert lim.hit("a", cost=99).cost == 3            # cost can't go above the limit


def test_rate_limit_headers_and_429(monkeypatch):
    monkeypatch.setattr(rate_limit, "limiter", SlidingWindowLimiter(2, 60))
    r1 = client.post(FORECAST, json=PARCEL)           # one parcel = one cell = one unit
    assert r1.status_code == 200
    assert r1.headers["X-RateLimit-Limit"] == "2" and r1.headers["X-RateLimit-Remaining"] == "1"
    assert r1.headers["X-RateLimit-Cost"] == "1"
    one = {"polygon": [{"lat": 20.9, "lon": -101.4}], "horizons": ["seasonal"]}
    assert client.post(FORECAST, json=one).status_code == 200
    r3 = client.post(FORECAST, json=one)
    assert r3.status_code == 429
    assert int(r3.headers["Retry-After"]) >= 1 and r3.headers["X-RateLimit-Remaining"] == "0"
    assert "Rate limit exceeded" in r3.json()["detail"]


def test_cost_is_distinct_cells(monkeypatch):
    monkeypatch.setattr(rate_limit, "limiter", SlidingWindowLimiter(10, 60))
    far = {"polygon": [{"lat": 20.0, "lon": -101.0}, {"lat": 21.0, "lon": -101.0}, {"lat": 22.0, "lon": -101.0}]}
    r = client.post(FORECAST, json={**far, "horizons": ["seasonal"]})
    assert r.headers["X-RateLimit-Cost"] == "3" and r.headers["X-RateLimit-Remaining"] == "7"


def test_health_is_not_rate_limited(monkeypatch):
    monkeypatch.setattr(rate_limit, "limiter", SlidingWindowLimiter(1, 60))
    for _ in range(5):
        assert client.get("/health").status_code == 200
    h = client.get("/health").json()
    assert h["port"] == get_settings().port and h["rate_limit"]["enabled"] is True


def test_proxy_header_is_ignored_unless_trusted(monkeypatch):
    monkeypatch.setattr(rate_limit, "limiter", SlidingWindowLimiter(1, 60))
    q = {"polygon": [{"lat": 20.9, "lon": -101.4}], "horizons": ["seasonal"]}
    assert client.post(FORECAST, json=q, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.post(FORECAST, json=q, headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


# config
def test_routes_follow_api_version_from_config():
    cfg = get_settings()
    assert cfg.api_prefix == f"/api/{cfg.api_version}"
    spec = client.get("/openapi.json").json()
    assert all(p == "/health" or p.startswith(cfg.api_prefix + "/") for p in spec["paths"])
    assert client.post(FORECAST, json=PARCEL).status_code == 200
    stale = "/api/v0/forecast" if cfg.api_version == "v1" else "/api/v1/forecast"
    assert client.post(stale, json=PARCEL).status_code == 404       # other versions are not served


def test_health_reports_config_values():
    cfg, h = get_settings(), client.get("/health").json()
    assert (h["service"], h["version"], h["api_version"], h["port"]) == (cfg.service_name, cfg.service_version, cfg.api_version, cfg.port)
    r = client.post(FORECAST, json=PARCEL).json()
    assert (r["service"], r["version"]) == (cfg.service_name, cfg.service_version)
    assert client.get("/openapi.json").json()["info"]["version"] == cfg.service_version


def test_settings_validation():
    assert Settings(api_version="v3").api_prefix == "/api/v3"
    for bad in ("3", "version2", "V1", ""):
        with pytest.raises(ValueError):
            Settings(api_version=bad)
    with pytest.raises(ValueError):
        Settings(port=70000)

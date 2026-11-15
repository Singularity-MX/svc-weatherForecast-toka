import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Query

from .schemas import (
    AnomaliaEstacionalItem,
    ForecastResponse,
    PoligonoForecastResponse,
    PoligonoRequest,
    PronosticoDiarioItem,
)

MODEL_BUNDLE_PATH = Path("models/toka_forecast_bundle.joblib")

CACHE_FORECAST: Dict[Tuple[float, float], Tuple[float, ForecastResponse]] = {}
CACHE_CLIMATOLOGIA: Dict[Tuple[float, float], dict] = {}
CACHE_TTL_SECONDS = 3600

app = FastAPI(
    title="svc-weatherForecast-toka",
    description="Microservicio de pronóstico meteorológico híbrido (ML + Ensambles Físicos) por coordenadas y polígonos.",
    version="1.2.0",
)

if not MODEL_BUNDLE_PATH.exists():
    raise RuntimeError(f"No se encontró {MODEL_BUNDLE_PATH}. Ejecuta primero src/entrenar_modelo.py")

BUNDLE = joblib.load(MODEL_BUNDLE_PATH)


def obtener_climatologia_coordenada(lat: float, lon: float) -> dict:
    clave = (round(lat, 2), round(lon, 2))
    if clave in CACHE_CLIMATOLOGIA:
        return CACHE_CLIMATOLOGIA[clave]

    dias_mes = {1: 31, 2: 28.25, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

    try:
        url = "https://power.larc.nasa.gov/api/temporal/monthly/point"
        params = {
            "parameters": "T2M,PRECTOTCORR",
            "community": "AG",
            "longitude": lon,
            "latitude": lat,
            "start": 1991,
            "end": 2020,
            "format": "JSON"
        }
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        p_data = resp.json()["properties"]["parameter"]

        df_h = pd.DataFrame({"T2M": p_data["T2M"], "PRECIP": p_data["PRECTOTCORR"]})
        df_h = df_h[~df_h.index.str.endswith("13")].replace(-999.0, np.nan)
        df_h["mes"] = df_h.index.str[-2:].astype(int)

        base = df_h.groupby("mes").mean()
        clim_data = {
            "temp_mes": base["T2M"].to_dict(),
            "precip_mes_mm": (base["PRECIP"] * base.index.map(dias_mes)).to_dict(),
            "fuente_clim": "NASA_POWER_1991_2020"
        }
    except Exception:
        clim_m = BUNDLE["clim_mensual"].groupby("mes").mean()
        clim_data = {
            "temp_mes": clim_m["temp_normal_mes"].to_dict(),
            "precip_mes_mm": (clim_m["precip_normal_dia"] * clim_m.index.map(dias_mes)).to_dict(),
            "fuente_clim": "BUNDLE_REGIONAL_FALLBACK"
        }

    CACHE_CLIMATOLOGIA[clave] = clim_data
    return clim_data


def obtener_pronostico_hibrido_7d(
    lat: float,
    lon: float,
    clim: dict
) -> Tuple[List[PronosticoDiarioItem], float, str]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": [
            "temperature_2m_max",
            "temperature_2m_min",
            "temperature_2m_mean",
            "precipitation_sum",
            "precipitation_probability_max",
            "relative_humidity_2m_mean",
            "shortwave_radiation_sum"
        ],
        "past_days": 8,
        "forecast_days": 8,
        "timezone": "auto"
    }

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    elevacion_real_m = float(payload.get("elevation", 1800.0))
    tz_nombre = payload.get("timezone", "America/Mexico_City")
    d = payload["daily"]

    df = pd.DataFrame({
        "fecha": pd.to_datetime(d["time"]),
        "temp_max_c": d["temperature_2m_max"],
        "temp_min_c": d["temperature_2m_min"],
        "temp_media_c": d["temperature_2m_mean"],
        "precipitacion_mm": d["precipitation_sum"],
        "prob_lluvia": d["precipitation_probability_max"],
        "humedad_rel_pct": d["relative_humidity_2m_mean"],
        "radiacion_mj_m2": d["shortwave_radiation_sum"],
    })

    df["precipitacion_mm"] = df["precipitacion_mm"].fillna(0.0)
    df["prob_lluvia"] = df["prob_lluvia"].fillna(0.0)
    df["humedad_rel_pct"] = df["humedad_rel_pct"].fillna(50.0)
    df["radiacion_mj_m2"] = df["radiacion_mj_m2"].fillna(18.0)

    df["altitud_m"] = elevacion_real_m
    df["dia_anio"] = df["fecha"].dt.dayofyear
    df["sin_doy"] = np.sin(2 * np.pi * df["dia_anio"] / 365.25)
    df["cos_doy"] = np.cos(2 * np.pi * df["dia_anio"] / 365.25)
    df["amplitud_termica_c"] = df["temp_max_c"] - df["temp_min_c"]

    for lag in [1, 2, 3]:
        df[f"temp_lag_{lag}"] = df["temp_media_c"].shift(lag)
        df[f"precip_lag_{lag}"] = df["precipitacion_mm"].shift(lag)
        df[f"hum_lag_{lag}"] = df["humedad_rel_pct"].shift(lag)

    df["temp_roll_7d"] = df["temp_media_c"].shift(1).rolling(7).mean()
    df["precip_roll_7d"] = df["precipitacion_mm"].shift(1).rolling(7).sum()
    df["fecha_objetivo"] = df["fecha"] + pd.Timedelta(days=1)

    hoy = pd.Timestamp(datetime.now(ZoneInfo(tz_nombre)).date())
    fin = hoy + pd.Timedelta(days=7)

    pred_rows = df[
        (df["fecha_objetivo"] >= hoy) &
        (df["fecha_objetivo"] < fin)
    ].copy().sort_values("fecha_objetivo").reset_index(drop=True)

    fechas_esperadas = pd.Series(pd.date_range(hoy, periods=7, freq="D"))
    if len(pred_rows) != 7 or not np.array_equal(
        pred_rows["fecha_objetivo"].to_numpy(dtype="datetime64[ns]"),
        fechas_esperadas.to_numpy(dtype="datetime64[ns]")
    ):
        raise ValueError("No se pudieron construir exactamente siete días consecutivos de características.")

    meteo_objetivo = df[[
        "fecha", "temp_max_c", "temp_min_c", "temp_media_c",
        "precipitacion_mm", "prob_lluvia", "humedad_rel_pct"
    ]].rename(columns={
        "fecha": "fecha_objetivo",
        "temp_max_c": "temp_max_obj_c",
        "temp_min_c": "temp_min_obj_c",
        "temp_media_c": "temp_media_obj_c",
        "precipitacion_mm": "precipitacion_obj_mm",
        "prob_lluvia": "prob_lluvia_obj",
        "humedad_rel_pct": "humedad_obj_pct"
    })

    pred_rows = pred_rows.merge(meteo_objetivo, on="fecha_objetivo", how="left", validate="one_to_one")
    X_inf = pred_rows[BUNDLE["features"]].copy()

    if X_inf.isna().any().any() or not np.isfinite(X_inf.to_numpy(dtype=float)).all():
        raise ValueError("Características inválidas o incompletas para inferencia.")

    pred_ml_temp = BUNDLE["modelo_temp"].predict(X_inf)
    pred_ml_prec = np.clip(BUNDLE["modelo_precip"].predict(X_inf), 0.0, None)

    temp_hibrida = np.clip(
        0.65 * pred_rows["temp_media_obj_c"].to_numpy() + 0.35 * pred_ml_temp,
        pred_rows["temp_min_obj_c"].to_numpy(),
        pred_rows["temp_max_obj_c"].to_numpy()
    )
    prec_hibrida = np.clip(
        0.70 * pred_rows["precipitacion_obj_mm"].to_numpy() + 0.30 * pred_ml_prec,
        0.0,
        None
    )

    resultados = []
    sigma_t = BUNDLE["std_residual_temp"]
    for i, row in enumerate(pred_rows.itertuples(index=False)):
        margen_90 = 1.64 * sigma_t * (1.0 + i * 0.12)
        t_val = float(temp_hibrida[i])
        t_clim = float(clim["temp_mes"].get(row.fecha_objetivo.month, t_val))

        resultados.append(PronosticoDiarioItem(
            fecha=row.fecha_objetivo.strftime("%Y-%m-%d"),
            temp_media_c=round(t_val, 2),
            temp_min_c=round(float(row.temp_min_obj_c), 2),
            temp_max_c=round(float(row.temp_max_obj_c), 2),
            anomalia_temp_c=round(t_val - t_clim, 2),
            incertidumbre_temp_c=[round(t_val - margen_90, 2), round(t_val + margen_90, 2)],
            precipitacion_mm=round(float(prec_hibrida[i]), 2),
            probabilidad_lluvia_pct=round(float(row.prob_lluvia_obj), 1),
            humedad_rel_pct=round(float(row.humedad_obj_pct), 1),
        ))

    return resultados, elevacion_real_m, tz_nombre


def obtener_anomalias_estacionales(lat: float, lon: float, clim: dict) -> List[AnomaliaEstacionalItem]:
    url = "https://seasonal-api.open-meteo.com/v1/seasonal"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ["temperature_2m_mean", "precipitation_sum"],
        "models": "ecmwf_seas5"
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    df_d = pd.DataFrame(resp.json()["daily"])
    df_d["time"] = pd.to_datetime(df_d["time"])

    cols_t = [c for c in df_d.columns if c.startswith("temperature_2m_mean")]
    cols_p = [c for c in df_d.columns if c.startswith("precipitation_sum")]
    df_d["t_mean"] = df_d[cols_t].mean(axis=1)
    df_d["p_sum"] = df_d[cols_p].mean(axis=1)
    df_d["periodo"] = df_d["time"].dt.to_period("M").astype(str)
    df_d["mes"] = df_d["time"].dt.month

    mensual = df_d.groupby(["periodo", "mes"]).agg(
        t_pred=("t_mean", "mean"),
        p_pred=("p_sum", "sum"),
        dias=("time", "count")
    ).reset_index()
    mensual = mensual[mensual["dias"] >= 25]

    salida = []
    for row in mensual.itertuples():
        t_norm = float(clim["temp_mes"][row.mes])
        p_norm = float(clim["precip_mes_mm"][row.mes])

        anom_t = float(row.t_pred) - t_norm
        anom_p_pct = ((float(row.p_pred) - p_norm) / max(p_norm, 1.0)) * 100.0

        etiqueta_t = "Cálido" if anom_t > 0.6 else ("Frío" if anom_t < -0.6 else "Térmicamente Normal")
        etiqueta_p = "Húmedo" if anom_p_pct > 15 else ("Seco" if anom_p_pct < -15 else "Lluvia Normal")

        salida.append(AnomaliaEstacionalItem(
            periodo=row.periodo,
            temp_normal_c=round(t_norm, 2),
            temp_pronostico_c=round(float(row.t_pred), 2),
            anomalia_temp_c=round(anom_t, 2),
            precip_normal_mm=round(p_norm, 1),
            precip_pronostico_mm=round(float(row.p_pred), 1),
            anomalia_precip_pct=round(anom_p_pct, 1),
            escenario=f"{etiqueta_t} / {etiqueta_p}"
        ))
    return salida


def _procesar_coordenada(lat: float, lon: float, vertice: int = 1) -> ForecastResponse:
    """Ejecuta o recupera de caché el pronóstico para un punto geográfico."""
    clave_cache = (round(lat, 2), round(lon, 2))
    ahora = time.time()

    if clave_cache in CACHE_FORECAST:
        expira, base_cache = CACHE_FORECAST[clave_cache]
        if ahora < expira:
            # Clonamos la respuesta cached ajustando el índice y coordenada exacta del vértice
            copia = base_cache.model_copy(deep=True)
            copia.vertice = vertice
            copia.ubicacion["latitud"] = lat
            copia.ubicacion["longitud"] = lon
            return copia

    clim = obtener_climatologia_coordenada(lat, lon)
    pronostico_7d, elevacion_m, tz_nombre = obtener_pronostico_hibrido_7d(lat, lon, clim)
    estacional = obtener_anomalias_estacionales(lat, lon, clim)

    respuesta = ForecastResponse(
        vertice=vertice,
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        ubicacion={
            "latitud": lat,
            "longitud": lon,
            "elevacion_dem_m": elevacion_m,
            "zona_horaria": tz_nombre,
            "climatologia_ref": clim["fuente_clim"]
        },
        metricas_modelo=BUNDLE["metricas"],
        pronostico_7_dias=pronostico_7d,
        clima_futuro_estacional=estacional
    )
    CACHE_FORECAST[clave_cache] = (ahora + CACHE_TTL_SECONDS, respuesta)
    return respuesta


# ==========================================
# ENDPOINTS DEL MICROSERVICIO (PORT 5000)
# ==========================================
@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "svc-weatherForecast-toka",
        "mode": "polygon_and_point",
        "port": 5000,
        "models_loaded": True
    }


@app.post("/api/v1/forecast", response_model=PoligonoForecastResponse)
@app.post("/api/v1/clima-futuro", response_model=PoligonoForecastResponse)
def post_weather_forecast_poligono(payload: PoligonoRequest):
    """
    Recibe un polígono de N vértices (ej. 4 coordenadas de parcela) con el mismo
    contrato que svc-weatherHistory-toka y devuelve el arreglo de pronósticos.
    """
    try:
        resultados = [
            _procesar_coordenada(punto.lat, punto.lon, vertice=idx)
            for idx, punto in enumerate(payload.poligono, start=1)
        ]
        return PoligonoForecastResponse(
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            total_vertices=len(resultados),
            resultados=resultados
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Error al consultar proveedor meteorológico upstream: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno en motor de pronóstico: {str(e)}")


@app.get("/api/v1/forecast", response_model=ForecastResponse)
def get_weather_forecast_puntual(
    lat: float = Query(..., ge=-90.0, le=90.0, description="Latitud exacta"),
    lon: float = Query(..., ge=-180.0, le=180.0, description="Longitud exacta")
):
    """Endpoint puntual de retrocompatibilidad para consultas de una sola coordenada."""
    try:
        return _procesar_coordenada(lat, lon, vertice=1)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Error al consultar proveedor meteorológico upstream: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno en motor de pronóstico: {str(e)}")
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
import joblib
import numpy as np
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .schemas import AnomaliaEstacionalItem, ForecastResponse, PronosticoDiarioItem# o desde donde tengas definidos tus modelos Pydantic

MODEL_BUNDLE_PATH = Path("models/toka_forecast_bundle.joblib")

import time
from typing import Dict, Tuple

# Caché en memoria: {(lat_redondeada, lon_redondeada): (timestamp_expiracion, ForecastResponse)}
CACHE_FORECAST: Dict[Tuple[float, float], Tuple[float, ForecastResponse]] = {}
CACHE_TTL_SECONDS = 3600

app = FastAPI(
    title="svc-weatherForecast-toka",
    description="Microservicio de pronóstico meteorológico híbrido (ML + Ensambles Físicos) y anomalías estacionales.",
    version="1.0.0",
)

# Cargar artefactos en memoria al arrancar el contenedor/proceso
if not MODEL_BUNDLE_PATH.exists():
    raise RuntimeError(f"No se encontró {MODEL_BUNDLE_PATH}. Ejecuta primero src/entrenar_modelo.py")

BUNDLE = joblib.load(MODEL_BUNDLE_PATH)



# ==========================================
# LÓGICA HIDROMETEOROLÓGICA HÍBRIDA
# ==========================================
def encontrar_nodo_cercano(lat: float, lon: float) -> dict:
    """Encuentra la estación calibrada más cercana en la base de conocimiento."""
    nodos = BUNDLE["ubicaciones"]
    mejor = min(nodos, key=lambda u: (u["latitud"] - lat) ** 2 + (u["longitud"] - lon) ** 2)
    return mejor


def obtener_pronostico_hibrido_7d(lat: float, lon: float, nodo: dict) -> List[PronosticoDiarioItem]:
    """Combina NWP (Open-Meteo) + corrección ML local (XGBoost/RF) para los próximos 7 días."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": [
            "temperature_2m_max", "temperature_2m_min", "temperature_2m_mean",
            "precipitation_sum", "precipitation_probability_max",
            "relative_humidity_2m_mean", "shortwave_radiation_sum"
        ],
        "past_days": 7,  # Necesarios para alimentar los rezagos (lags) del modelo ML
        "forecast_days": 8,
        "timezone": "America/Mexico_City"
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    d = resp.json()["daily"]

    df = pd.DataFrame({
        "fecha": pd.to_datetime(d["time"]),
        "temp_max_c": d["temperature_2m_max"],
        "temp_min_c": d["temperature_2m_min"],
        "temp_media_c": d["temperature_2m_mean"],
        "precipitacion_mm": np.nan_to_num(d["precipitation_sum"], nan=0.0),
        "prob_lluvia": np.nan_to_num(d["precipitation_probability_max"], nan=0.0),
        "humedad_rel_pct": np.nan_to_num(d["relative_humidity_2m_mean"], nan=50.0),
        "radiacion_mj_m2": np.nan_to_num(d["shortwave_radiation_sum"], nan=18.0),
    })

    df["altitud_m"] = nodo["altitud_m"]
    df["dia_anio"] = df["fecha"].dt.dayofyear
    df["sin_doy"] = np.sin(2 * np.pi * df["dia_anio"] / 365.25)
    df["cos_doy"] = np.cos(2 * np.pi * df["dia_anio"] / 365.25)
    df["amplitud_termica_c"] = df["temp_max_c"] - df["temp_min_c"]

    for lag in [1, 2, 3]:
        df[f"temp_lag_{lag}"] = df["temp_media_c"].shift(lag)
        df[f"precip_lag_{lag}"] = df["precipitacion_mm"].shift(lag)
        df[f"hum_lag_{lag}"] = df["humedad_rel_pct"].shift(lag)

    df["temp_roll_7d"] = df["temp_media_c"].shift(1).rolling(7, min_periods=1).mean()
    df["precip_roll_7d"] = df["precipitacion_mm"].shift(1).rolling(7, min_periods=1).sum()

    # Filtrar solo los próximos 7 días a partir de hoy
    hoy_str = datetime.now().strftime("%Y-%m-%d")
    df_fut = df[df["fecha"] >= hoy_str].head(7).copy().bfill()

    X_inf = df_fut[BUNDLE["features"]]
    pred_ml_temp = BUNDLE["modelo_temp"].predict(X_inf)
    pred_ml_prec = np.clip(BUNDLE["modelo_precip"].predict(X_inf), 0.0, None)

    # Ensamble híbrido: 65% modelo físico global + 35% ajuste estadístico local
    temp_hibrida = 0.65 * df_fut["temp_media_c"].values + 0.35 * pred_ml_temp
    prec_hibrida = 0.70 * df_fut["precipitacion_mm"].values + 0.30 * pred_ml_prec

    clim_nodo = BUNDLE["clim_diaria"][BUNDLE["clim_diaria"]["id_ubicacion"] == nodo["id_ubicacion"]]
    mapa_clim = dict(zip(clim_nodo["dia_anio"], clim_nodo["temp_media_c"]))

    resultados = []
    sigma_t = BUNDLE["std_residual_temp"]
    for i, row in enumerate(df_fut.itertuples()):
        # La incertidumbre crece ligeramente con cada día de anticipación
        factor_horizonte = 1.0 + (i * 0.12)
        margen_90 = 1.64 * sigma_t * factor_horizonte

        t_val = float(temp_hibrida[i])
        t_clim = float(mapa_clim.get(row.dia_anio, t_val))
        p_val = float(prec_hibrida[i])

        resultados.append(PronosticoDiarioItem(
            fecha=row.fecha.strftime("%Y-%m-%d"),
            temp_media_c=round(t_val, 2),
            temp_min_c=round(float(row.temp_min_c), 2),
            temp_max_c=round(float(row.temp_max_c), 2),
            anomalia_temp_c=round(t_val - t_clim, 2),
            incertidumbre_temp_c=[round(t_val - margen_90, 2), round(t_val + margen_90, 2)],
            precipitacion_mm=round(p_val, 2),
            probabilidad_lluvia_pct=round(float(row.prob_lluvia), 1),
            humedad_rel_pct=round(float(row.humedad_rel_pct), 1),
        ))
    return resultados


def obtener_anomalias_estacionales(lat: float, lon: float, nodo: dict) -> List[AnomaliaEstacionalItem]:
    """Calcula el pronóstico estacional (6 meses) y anomalías contra la línea base local."""
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

    clim_m = BUNDLE["clim_mensual"][BUNDLE["clim_mensual"]["id_ubicacion"] == nodo["id_ubicacion"]]
    dias_mes = {1: 31, 2: 28.25, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

    salida = []
    for row in mensual.itertuples():
        ref = clim_m[clim_m["mes"] == row.mes].iloc[0]
        t_norm = float(ref["temp_normal_mes"])
        p_norm = float(ref["precip_normal_dia"]) * dias_mes[row.mes]

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


# ==========================================
# ENDPOINTS DEL MICROSERVICIO (PORT 5000)
# ==========================================
@app.get("/health")
def health_check():
    """Endpoint de monitoreo para NGINX / Docker / svc-dataIntegration-toka."""
    return {
        "status": "ok",
        "service": "svc-weatherForecast-toka",
        "port": 5000,
        "models_loaded": True
    }


@app.get("/api/v1/forecast", response_model=ForecastResponse)
def get_weather_forecast(
    lat: float = Query(21.1220, description="Latitud de la parcela o punto de estudio"),
    lon: float = Query(-101.6820, description="Longitud de la parcela o punto de estudio")
):
    clave_cache = (round(lat, 2), round(lon, 2))
    ahora = time.time()

    if clave_cache in CACHE_FORECAST:
        expira, respuesta_cacheada = CACHE_FORECAST[clave_cache]
        if ahora < expira:
            return respuesta_cacheada

    try:
        nodo = encontrar_nodo_cercano(lat, lon)
        pronostico_7d = obtener_pronostico_hibrido_7d(lat, lon, nodo)
        estacional = obtener_anomalias_estacionales(lat, lon, nodo)

        respuesta = ForecastResponse(
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            ubicacion={
                "lat_solicitada": lat,
                "lon_solicitada": lon,
                "nodo_calibracion": nodo["nombre"],
                "altitud_ref_m": nodo["altitud_m"]
            },
            metricas_modelo=BUNDLE["metricas"],
            pronostico_7_dias=pronostico_7d,
            clima_futuro_estacional=estacional
        )
        CACHE_FORECAST[clave_cache] = (ahora + CACHE_TTL_SECONDS, respuesta)
        return respuesta
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Error al consultar proveedor meteorológico upstream: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno en motor de pronóstico: {str(e)}")
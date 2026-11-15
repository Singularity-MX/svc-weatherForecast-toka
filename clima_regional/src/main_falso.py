from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import List, Optional
import joblib
import numpy as np
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .schemas import AnomaliaEstacionalItem, ForecastResponse, PronosticoDiarioItem

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
    """
    Combina el pronóstico físico de Open-Meteo con el modelo ML.

    El modelo fue entrenado para:
        características del día t -> objetivo del día t+1.

    Por tanto, las características se toman del día anterior y se
    combinan con el pronóstico físico de la fecha objetivo.
    """
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
        # Ocho días previos permiten construir la ventana móvil
        # de siete días para las características del día anterior.
        "past_days": 8,
        "forecast_days": 8,
        "timezone": "America/Mexico_City"
    }

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    d = resp.json()["daily"]

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

    # Los valores auxiliares conservan los valores por defecto
    # que se utilizaron en la versión anterior del servicio.
    df["precipitacion_mm"] = df["precipitacion_mm"].fillna(0.0)
    df["prob_lluvia"] = df["prob_lluvia"].fillna(0.0)
    df["humedad_rel_pct"] = df["humedad_rel_pct"].fillna(50.0)
    df["radiacion_mj_m2"] = df["radiacion_mj_m2"].fillna(18.0)

    df["altitud_m"] = nodo["altitud_m"]
    df["dia_anio"] = df["fecha"].dt.dayofyear
    df["sin_doy"] = np.sin(2 * np.pi * df["dia_anio"] / 365.25)
    df["cos_doy"] = np.cos(2 * np.pi * df["dia_anio"] / 365.25)
    df["amplitud_termica_c"] = df["temp_max_c"] - df["temp_min_c"]

    # Ingeniería de características idéntica a la del entrenamiento.
    for lag in [1, 2, 3]:
        df[f"temp_lag_{lag}"] = df["temp_media_c"].shift(lag)
        df[f"precip_lag_{lag}"] = df["precipitacion_mm"].shift(lag)
        df[f"hum_lag_{lag}"] = df["humedad_rel_pct"].shift(lag)

    # Se requieren las siete observaciones anteriores, igual que en training.
    df["temp_roll_7d"] = df["temp_media_c"].shift(1).rolling(7).mean()
    df["precip_roll_7d"] = df["precipitacion_mm"].shift(1).rolling(7).sum()

    # Cada fila de características del día t predice el día t+1.
    df["fecha_objetivo"] = df["fecha"] + pd.Timedelta(days=1)

    hoy = pd.Timestamp(datetime.now(ZoneInfo("America/Mexico_City")).date())
    fin = hoy + pd.Timedelta(days=7)

    pred_rows = df[
        (df["fecha_objetivo"] >= hoy) &
        (df["fecha_objetivo"] < fin)
    ].copy()

    pred_rows = pred_rows.sort_values("fecha_objetivo").reset_index(drop=True)

    fechas_esperadas = pd.Series(pd.date_range(hoy, periods=7, freq="D"))

    fechas_obtenidas = pred_rows["fecha_objetivo"].to_numpy(dtype="datetime64[ns]")
    fechas_requeridas = fechas_esperadas.to_numpy(dtype="datetime64[ns]")

    if len(pred_rows) != 7 or not np.array_equal(fechas_obtenidas, fechas_requeridas):
        raise ValueError(
            "No se pudieron construir exactamente siete días consecutivos de características."
        )

    # Recuperar el pronóstico físico de la fecha objetivo.
    meteo_objetivo = df[
        [
            "fecha",
            "temp_max_c",
            "temp_min_c",
            "temp_media_c",
            "precipitacion_mm",
            "prob_lluvia",
            "humedad_rel_pct"
        ]
    ].rename(columns={
        "fecha": "fecha_objetivo",
        "temp_max_c": "temp_max_obj_c",
        "temp_min_c": "temp_min_obj_c",
        "temp_media_c": "temp_media_obj_c",
        "precipitacion_mm": "precipitacion_obj_mm",
        "prob_lluvia": "prob_lluvia_obj",
        "humedad_rel_pct": "humedad_obj_pct"
    })

    pred_rows = pred_rows.merge(
        meteo_objetivo,
        on="fecha_objetivo",
        how="left",
        validate="one_to_one"
    )

    features = BUNDLE["features"]
    X_inf = pred_rows[features].copy()

    if X_inf.isna().any().any():
        columnas = X_inf.columns[X_inf.isna().any()].tolist()
        raise ValueError(f"Hay características faltantes para inferencia: {columnas}")

    if not np.isfinite(X_inf.to_numpy(dtype=float)).all():
        raise ValueError("Se detectaron valores no finitos en las características de inferencia.")

    columnas_objetivo = [
        "temp_max_obj_c",
        "temp_min_obj_c",
        "temp_media_obj_c",
        "precipitacion_obj_mm",
        "prob_lluvia_obj",
        "humedad_obj_pct"
    ]

    if pred_rows[columnas_objetivo].isna().any().any():
        raise ValueError("Open-Meteo devolvió valores faltantes en el pronóstico objetivo.")

    pred_ml_temp = BUNDLE["modelo_temp"].predict(X_inf)
    pred_ml_prec = np.clip(
        BUNDLE["modelo_precip"].predict(X_inf),
        0.0,
        None
    )

    # Ensamble: cada componente representa la misma fecha objetivo.
    temp_hibrida = (
        0.65 * pred_rows["temp_media_obj_c"].to_numpy() +
        0.35 * pred_ml_temp
    )

    prec_hibrida = (
        0.70 * pred_rows["precipitacion_obj_mm"].to_numpy() +
        0.30 * pred_ml_prec
    )

    # Mantener la temperatura media dentro del rango físico diario.
    temp_hibrida = np.clip(
        temp_hibrida,
        pred_rows["temp_min_obj_c"].to_numpy(),
        pred_rows["temp_max_obj_c"].to_numpy()
    )

    prec_hibrida = np.clip(prec_hibrida, 0.0, None)

    clim_nodo = BUNDLE["clim_diaria"][
        BUNDLE["clim_diaria"]["id_ubicacion"] == nodo["id_ubicacion"]
    ]

    mapa_clim = dict(
        zip(clim_nodo["dia_anio"], clim_nodo["temp_media_c"])
    )

    resultados = []
    sigma_t = BUNDLE["std_residual_temp"]

    for i, row in enumerate(pred_rows.itertuples(index=False)):
        factor_horizonte = 1.0 + (i * 0.12)
        margen_90 = 1.64 * sigma_t * factor_horizonte

        t_val = float(temp_hibrida[i])
        t_clim = float(mapa_clim.get(
            row.fecha_objetivo.dayofyear,
            t_val
        ))

        resultados.append(PronosticoDiarioItem(
            fecha=row.fecha_objetivo.strftime("%Y-%m-%d"),
            temp_media_c=round(t_val, 2),
            temp_min_c=round(float(row.temp_min_obj_c), 2),
            temp_max_c=round(float(row.temp_max_obj_c), 2),
            anomalia_temp_c=round(t_val - t_clim, 2),
            incertidumbre_temp_c=[
                round(t_val - margen_90, 2),
                round(t_val + margen_90, 2)
            ],
            precipitacion_mm=round(float(prec_hibrida[i]), 2),
            probabilidad_lluvia_pct=round(float(row.prob_lluvia_obj), 1),
            humedad_rel_pct=round(float(row.humedad_obj_pct), 1),
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
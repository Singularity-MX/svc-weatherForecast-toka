import time
import sqlite3
import requests
import pandas as pd
from datetime import datetime
from database import DB_PATH, inicializar_db

# Malla de coordenadas de calibración espacial (puedes agregar cualquier punto lat/lon)
PUNTOS_CALIBRACION = [
    {"id": "PT_21.12_-101.68", "lat": 21.1220, "lon": -101.6820},
    {"id": "PT_20.68_-101.36", "lat": 20.6767, "lon": -101.3563},
    {"id": "PT_20.52_-100.82", "lat": 20.5235, "lon": -100.8157},
    {"id": "PT_21.02_-101.26", "lat": 21.0190, "lon": -101.2574},
]


def ya_descargado(conn: sqlite3.Connection, id_ubicacion: str, fuente: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM historico_diario WHERE id_ubicacion = ? AND fuente = ?",
        (id_ubicacion, fuente)
    )
    return cur.fetchone()[0] > 5000


def descargar_historico_open_meteo(conn: sqlite3.Connection, punto: dict, start_date: str, end_date: str) -> float:
    """
    Descarga series históricas desde Open-Meteo Archive para la coordenada exacta
    y devuelve la elevación DEM real del terreno en metros.
    """
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": punto["lat"],
        "longitude": punto["lon"],
        "start_date": start_date,
        "end_date": end_date,
        "daily": [
            "temperature_2m_max",
            "temperature_2m_min",
            "temperature_2m_mean",
            "precipitation_sum",
            "relative_humidity_2m_mean",
            "shortwave_radiation_sum"
        ],
        "timezone": "auto"
    }

    for intento in range(3):
        resp = requests.get(url, params=params, timeout=45)
        if resp.status_code == 429:
            espera = 35 * (intento + 1)
            print(f"[Open-Meteo] Límite 429 en ({punto['lat']}, {punto['lon']}). Esperando {espera}s...")
            time.sleep(espera)
            continue
        resp.raise_for_status()
        payload = resp.json()
        break
    else:
        raise RuntimeError(f"Open-Meteo rechazó ({punto['lat']}, {punto['lon']}) tras 3 intentos.")

    elevacion_dem = float(payload.get("elevation", 1800.0))

    if ya_descargado(conn, punto["id"], "OPEN_METEO_ARCHIVE"):
        print(f"[Open-Meteo] ({punto['lat']}, {punto['lon']}): Ya existe en DB, omitiendo inserción.")
        return elevacion_dem

    daily = payload["daily"]
    df = pd.DataFrame({
        "id_ubicacion": punto["id"],
        "fecha": daily["time"],
        "fuente": "OPEN_METEO_ARCHIVE",
        "temp_max_c": daily["temperature_2m_max"],
        "temp_min_c": daily["temperature_2m_min"],
        "temp_media_c": daily["temperature_2m_mean"],
        "precipitacion_mm": daily["precipitation_sum"],
        "humedad_rel_pct": daily["relative_humidity_2m_mean"],
        "radiacion_mj_m2": daily["shortwave_radiation_sum"],
    })

    df.to_sql("historico_diario", conn, if_exists="append", index=False, method="multi")
    print(f"[Open-Meteo] ({punto['lat']}, {punto['lon']}) [DEM: {elevacion_dem}m]: {len(df)} registros insertados.")
    time.sleep(8)
    return elevacion_dem


def descargar_nasa_power_diario(conn: sqlite3.Connection, punto: dict, start_date: str, end_date: str):
    """Descarga serie histórica satelital/reasimilación de NASA POWER para la coordenada exacta."""
    if ya_descargado(conn, punto["id"], "NASA_POWER"):
        print(f"[NASA POWER] ({punto['lat']}, {punto['lon']}): Ya existe en DB, omitiendo.")
        return

    url = "https://power.larc.nasa.gov/api/temporal/daily/point"
    params = {
        "parameters": "T2M_MAX,T2M_MIN,T2M,PRECTOTCORR,RH2M,ALLSKY_SFC_SW_DWN",
        "community": "AG",
        "longitude": punto["lon"],
        "latitude": punto["lat"],
        "start": start_date.replace("-", ""),
        "end": end_date.replace("-", ""),
        "format": "JSON"
    }
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    p = resp.json()["properties"]["parameter"]

    fechas = list(p["T2M"].keys())
    df = pd.DataFrame({
        "id_ubicacion": punto["id"],
        "fecha": pd.to_datetime(fechas, format="%Y%m%d").strftime("%Y-%m-%d"),
        "fuente": "NASA_POWER",
        "temp_max_c": [p["T2M_MAX"][f] for f in fechas],
        "temp_min_c": [p["T2M_MIN"][f] for f in fechas],
        "temp_media_c": [p["T2M"][f] for f in fechas],
        "precipitacion_mm": [p["PRECTOTCORR"][f] for f in fechas],
        "humedad_rel_pct": [p["RH2M"][f] for f in fechas],
        "radiacion_mj_m2": [p["ALLSKY_SFC_SW_DWN"][f] for f in fechas],
    }).replace(-999.0, None)

    df.to_sql("historico_diario", conn, if_exists="append", index=False, method="multi")
    print(f"[NASA POWER] ({punto['lat']}, {punto['lon']}): {len(df)} registros diarios insertados.")


def registrar_punto(conn: sqlite3.Connection, punto: dict, elevacion_m: float):
    etiqueta_coord = f"({punto['lat']:.4f}, {punto['lon']:.4f})"
    conn.execute("""
        INSERT OR REPLACE INTO ubicaciones (id_ubicacion, nombre, latitud, longitud, altitud_m, fuente_referencia)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (punto["id"], etiqueta_coord, punto["lat"], punto["lon"], elevacion_m, "GRID_COORDINATE_DEM"))
    conn.commit()


def ejecutar_pipeline_fase1():
    inicializar_db()
    conn = sqlite3.connect(DB_PATH)
    hoy = datetime.now().strftime("%Y-%m-%d")

    for pt in PUNTOS_CALIBRACION:
        elevacion = descargar_historico_open_meteo(conn, pt, start_date="2010-01-01", end_date=hoy)
        registrar_punto(conn, pt, elevacion)
        descargar_nasa_power_diario(conn, pt, start_date="2010-01-01", end_date=hoy)

    df_export = pd.read_sql_query("SELECT * FROM historico_diario", conn)
    df_export.to_parquet("data/dataset_inicial_fase1.parquet", index=False)
    print(f"\n[Entregable Fase 1] Completado: {len(df_export)} filas por coordenadas en data/dataset_inicial_fase1.parquet")
    conn.close()


if __name__ == "__main__":
    ejecutar_pipeline_fase1()
import sqlite3
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

DB_PATH = Path("data/clima_regional.db")
REPORTS_DIR = Path("reports/fase2")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

plt.rcParams["figure.dpi"] = 140
sns.set_theme(style="whitegrid", context="notebook")


def cargar_y_homologar(conn: sqlite3.Connection) -> pd.DataFrame:
    """Extrae datos crudos por coordenada, homologa calendario diario y aplica límites físicos."""
    df = pd.read_sql_query("""
        SELECT h.*, u.latitud, u.longitud, u.altitud_m
        FROM historico_diario h
        JOIN ubicaciones u ON h.id_ubicacion = u.id_ubicacion
    """, conn)

    df["fecha"] = pd.to_datetime(df["fecha"])
    df["coord_label"] = df.apply(lambda r: f"({r['latitud']:.2f}, {r['longitud']:.2f})", axis=1)

    df["humedad_rel_pct"] = df["humedad_rel_pct"].clip(lower=0.0, upper=100.0)
    df["precipitacion_mm"] = df["precipitacion_mm"].clip(lower=0.0)
    df["radiacion_mj_m2"] = df["radiacion_mj_m2"].clip(lower=0.0, upper=45.0)

    t_min_real = df[["temp_min_c", "temp_media_c", "temp_max_c"]].min(axis=1)
    t_max_real = df[["temp_min_c", "temp_media_c", "temp_max_c"]].max(axis=1)
    df["temp_min_c"] = t_min_real
    df["temp_max_c"] = t_max_real
    df["temp_media_c"] = df["temp_media_c"].fillna((df["temp_min_c"] + df["temp_max_c"]) / 2.0)

    dfs_continuos = []
    cols_grupo = ["id_ubicacion", "coord_label", "latitud", "longitud", "altitud_m", "fuente"]
    for (id_ub, label, lat, lon, alt, fuente), grupo in df.groupby(cols_grupo):
        rango_fechas = pd.date_range(grupo["fecha"].min(), grupo["fecha"].max(), freq="D")
        g = grupo.set_index("fecha").reindex(rango_fechas)
        g.index.name = "fecha"
        g["id_ubicacion"] = id_ub
        g["coord_label"] = label
        g["latitud"] = lat
        g["longitud"] = lon
        g["altitud_m"] = alt
        g["fuente"] = fuente
        dfs_continuos.append(g.reset_index())

    return pd.concat(dfs_continuos, ignore_index=True)


def limpiar_e_imputar(df: pd.DataFrame) -> pd.DataFrame:
    """Consolida fuentes satelitales y de reanálisis por punto geográfico."""
    vars_met = ["temp_max_c", "temp_min_c", "temp_media_c", "precipitacion_mm", "humedad_rel_pct", "radiacion_mj_m2"]
    idx_cols = ["id_ubicacion", "coord_label", "latitud", "longitud", "altitud_m", "fecha"]
    df_piv = df.pivot_table(index=idx_cols, columns="fuente", values=vars_met)

    df_consolidado = pd.DataFrame(index=df_piv.index).reset_index()

    for var in vars_met:
        sub = df_piv[var]
        serie = pd.Series(np.nan, index=sub.index)
        for fuente_pref in ["NASA_POWER", "OPEN_METEO_ARCHIVE", "CONAGUA_SMN"]:
            if fuente_pref in sub.columns:
                serie = sub[fuente_pref].combine_first(serie)
        df_consolidado[var] = serie.values

    df_consolidado = df_consolidado.sort_values(["id_ubicacion", "fecha"]).reset_index(drop=True)

    for col in ["temp_max_c", "temp_min_c", "temp_media_c", "humedad_rel_pct", "radiacion_mj_m2"]:
        df_consolidado[col] = (
            df_consolidado.groupby("id_ubicacion")[col]
            .transform(lambda s: s.interpolate(method="linear", limit=5).bfill().ffill())
        )
    df_consolidado["precipitacion_mm"] = df_consolidado["precipitacion_mm"].fillna(0.0)

    df_consolidado["amplitud_termica_c"] = df_consolidado["temp_max_c"] - df_consolidado["temp_min_c"]
    df_consolidado["mes"] = df_consolidado["fecha"].dt.month
    df_consolidado["dia_anio"] = df_consolidado["fecha"].dt.dayofyear

    return df_consolidado


def detectar_anomalias(df: pd.DataFrame) -> pd.DataFrame:
    """Calcula anomalías climatológicas respecto al ciclo normal diario por coordenada."""
    stats_temp = df.groupby(["id_ubicacion", "dia_anio"])["temp_media_c"].agg(
        temp_climatologia_c="mean",
        temp_std_clim="std"
    ).reset_index()

    stats_precip = df.groupby(["id_ubicacion", "mes"])["precipitacion_mm"].agg(
        precip_media_mes_mm="mean",
        precip_std_mes_mm="std"
    ).reset_index()

    df = df.merge(stats_temp, on=["id_ubicacion", "dia_anio"], how="left")
    df = df.merge(stats_precip, on=["id_ubicacion", "mes"], how="left")

    df["anomalia_temp_c"] = df["temp_media_c"] - df["temp_climatologia_c"]
    df["zscore_temp"] = df["anomalia_temp_c"] / df["temp_std_clim"].replace(0, np.nan)
    df["anomalia_precip_mm"] = df["precipitacion_mm"] - df["precip_media_mes_mm"]
    df["zscore_precip"] = df["anomalia_precip_mm"] / df["precip_std_mes_mm"].replace(0, np.nan)

    df["es_anomalia_termica"] = (df["zscore_temp"].abs() > 2.5).astype(int)
    df["es_lluvia_extrema"] = (df["zscore_precip"] > 3.0).astype(int)
    return df


def analizar_y_graficar(df: pd.DataFrame):
    """Genera gráficas diagnósticas etiquetadas por coordenada."""
    vars_corr = [
        "temp_max_c", "temp_min_c", "temp_media_c", "amplitud_termica_c",
        "precipitacion_mm", "humedad_rel_pct", "radiacion_mj_m2", "anomalia_temp_c"
    ]
    corr = df[vars_corr].corr()

    fig, ax = plt.subplots(figsize=(9, 7))
    sns.heatmap(corr, annot=True, fmt=".2f", cmap="RdBu_r", vmin=-1, vmax=1, center=0, ax=ax)
    ax.set_title("Correlación entre Variables Meteorológicas (Malla de Coordenadas)")
    plt.tight_layout()
    plt.savefig(REPORTS_DIR / "01_matriz_correlacion.png")
    plt.close()

    resumen_mensual = df.groupby(["coord_label", "mes"]).agg(
        temp_media=("temp_media_c", "mean"),
        precip_mensual=("precipitacion_mm", lambda s: s.mean() * 30.4)
    ).reset_index()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    sns.lineplot(data=resumen_mensual, x="mes", y="temp_media", hue="coord_label", marker="o", ax=ax1)
    ax1.set_ylabel("Temperatura Media (°C)")
    ax1.set_title("Comportamiento Estacional Histórico por Coordenada (2010–2026)")

    sns.barplot(data=resumen_mensual, x="mes", y="precip_mensual", hue="coord_label", ax=ax2)
    ax2.set_ylabel("Precipitación Mensual Promedio (mm)")
    ax2.set_xlabel("Mes del Año")
    ax2.set_xticks(range(0, 12))
    ax2.set_xticklabels(["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"])
    plt.tight_layout()
    plt.savefig(REPORTS_DIR / "02_ciclo_estacional.png")
    plt.close()


def ejecutar_fase2():
    conn = sqlite3.connect(DB_PATH)
    df_homologado = cargar_y_homologar(conn)
    df_limpio = limpiar_e_imputar(df_homologado)
    df_final = detectar_anomalias(df_limpio)

    df_save = df_final.copy()
    df_save["fecha"] = df_save["fecha"].dt.strftime("%Y-%m-%d")
    df_save.to_sql("historico_normalizado", conn, if_exists="replace", index=False)
    df_save.to_parquet("data/dataset_normalizado_fase2.parquet", index=False)

    analizar_y_graficar(df_final)
    conn.close()
    print(f"[Entregable Fase 2] Completado: {len(df_save)} registros normalizados por coordenada.")


if __name__ == "__main__":
    ejecutar_fase2()
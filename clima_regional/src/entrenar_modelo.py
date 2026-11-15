import sqlite3
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error
from xgboost import XGBRegressor

DB_PATH = Path("data/clima_regional.db")
MODEL_BUNDLE_PATH = Path("models/toka_forecast_bundle.joblib")


def construir_features(df: pd.DataFrame) -> pd.DataFrame:
    """Genera variables físicas, cíclicas y rezagos (lags) agnósticos a nombres de municipios."""
    df = df.sort_values(["id_ubicacion", "fecha"]).copy()
    df["fecha"] = pd.to_datetime(df["fecha"])

    df["sin_doy"] = np.sin(2 * np.pi * df["dia_anio"] / 365.25)
    df["cos_doy"] = np.cos(2 * np.pi * df["dia_anio"] / 365.25)

    for lag in [1, 2, 3]:
        df[f"temp_lag_{lag}"] = df.groupby("id_ubicacion")["temp_media_c"].shift(lag)
        df[f"precip_lag_{lag}"] = df.groupby("id_ubicacion")["precipitacion_mm"].shift(lag)
        df[f"hum_lag_{lag}"] = df.groupby("id_ubicacion")["humedad_rel_pct"].shift(lag)

    df["temp_roll_7d"] = df.groupby("id_ubicacion")["temp_media_c"].transform(lambda s: s.shift(1).rolling(7).mean())
    df["precip_roll_7d"] = df.groupby("id_ubicacion")["precipitacion_mm"].transform(lambda s: s.shift(1).rolling(7).sum())

    df["target_temp"] = df.groupby("id_ubicacion")["temp_media_c"].shift(-1)
    df["target_precip"] = df.groupby("id_ubicacion")["precipitacion_mm"].shift(-1)

    return df.dropna().reset_index(drop=True)


def entrenar_y_guardar():
    conn = sqlite3.connect(DB_PATH)
    df_raw = pd.read_sql_query("SELECT * FROM historico_normalizado", conn)
    conn.close()

    df = construir_features(df_raw)

    features = [
        "altitud_m", "sin_doy", "cos_doy",
        "temp_media_c", "amplitud_termica_c", "humedad_rel_pct", "radiacion_mj_m2", "precipitacion_mm",
        "temp_lag_1", "temp_lag_2", "temp_lag_3",
        "precip_lag_1", "precip_lag_2", "precip_lag_3",
        "hum_lag_1", "hum_lag_2", "hum_lag_3",
        "temp_roll_7d", "precip_roll_7d"
    ]

    df["fecha_objetivo"] = df["fecha"] + pd.Timedelta(days=1)
    fecha_corte = df["fecha_objetivo"].quantile(0.80)
    train = df[df["fecha_objetivo"] <= fecha_corte]
    test = df[df["fecha_objetivo"] > fecha_corte]

    if train.empty or test.empty:
        raise RuntimeError("La división temporal produjo un conjunto de entrenamiento o prueba vacío.")

    X_train, X_test = train[features], test[features]

    # --- 1. MODELO DE TEMPERATURA ---
    rf_temp = RandomForestRegressor(n_estimators=120, max_depth=12, n_jobs=-1, random_state=42)
    xgb_temp = XGBRegressor(n_estimators=250, max_depth=6, learning_rate=0.05, subsample=0.8, random_state=42, n_jobs=-1)

    rf_temp.fit(X_train, train["target_temp"])
    xgb_temp.fit(X_train, train["target_temp"])

    mae_rf_t = mean_absolute_error(test["target_temp"], rf_temp.predict(X_test))
    mae_xgb_t = mean_absolute_error(test["target_temp"], xgb_temp.predict(X_test))
    mejor_temp = xgb_temp if mae_xgb_t <= mae_rf_t else rf_temp
    std_residual_temp = float(np.std(test["target_temp"] - mejor_temp.predict(X_test)))

    print(f"[Temp] MAE Random Forest: {mae_rf_t:.3f}°C | MAE XGBoost: {mae_xgb_t:.3f}°C")

    # --- 2. MODELO DE PRECIPITACIÓN ---
    rf_prec = RandomForestRegressor(n_estimators=120, max_depth=10, n_jobs=-1, random_state=42)
    xgb_prec = XGBRegressor(n_estimators=250, max_depth=5, learning_rate=0.04, subsample=0.8, random_state=42, n_jobs=-1)

    rf_prec.fit(X_train, train["target_precip"])
    xgb_prec.fit(X_train, train["target_precip"])

    mae_rf_p = mean_absolute_error(test["target_precip"], np.clip(rf_prec.predict(X_test), 0, None))
    mae_xgb_p = mean_absolute_error(test["target_precip"], np.clip(xgb_prec.predict(X_test), 0, None))
    mejor_prec = xgb_prec if mae_xgb_p <= mae_rf_p else rf_prec
    std_residual_prec = float(np.std(test["target_precip"] - np.clip(mejor_prec.predict(X_test), 0, None)))

    print(f"[Precip] MAE Random Forest: {mae_rf_p:.3f} mm | MAE XGBoost: {mae_xgb_p:.3f} mm")

    # Climatología regional de respaldo en caso de caída temporal de NASA POWER
    clim_mensual = df_raw.groupby(["id_ubicacion", "mes"]).agg(
        temp_normal_mes=("temp_media_c", "mean"),
        precip_normal_dia=("precipitacion_mm", "mean")
    ).reset_index()

    bundle = {
        "modelo_temp": mejor_temp,
        "modelo_precip": mejor_prec,
        "features": features,
        "std_residual_temp": std_residual_temp,
        "std_residual_prec": std_residual_prec,
        "metricas": {
            "temp_mae_c": round(min(mae_rf_t, mae_xgb_t), 3),
            "precip_mae_mm": round(min(mae_rf_p, mae_xgb_p), 3),
        },
        "clim_mensual": clim_mensual,
    }

    joblib.dump(bundle, MODEL_BUNDLE_PATH)
    print(f"\n[Fase 3 Completada] Bundle generalizable serializado en {MODEL_BUNDLE_PATH}")


if __name__ == "__main__":
    entrenar_y_guardar()
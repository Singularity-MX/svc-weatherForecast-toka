import sqlite3
from pathlib import Path

DB_PATH = Path("data/clima_regional.db")


def inicializar_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS ubicaciones (
        id_ubicacion TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        latitud REAL NOT NULL,
        longitud REAL NOT NULL,
        altitud_m REAL,
        fuente_referencia TEXT
    );

    CREATE TABLE IF NOT EXISTS historico_diario (
        id_ubicacion TEXT NOT NULL,
        fecha DATE NOT NULL,
        fuente TEXT NOT NULL,          -- 'OPEN_METEO_ARCHIVE' | 'CONAGUA_SMN'
        temp_max_c REAL,
        temp_min_c REAL,
        temp_media_c REAL,
        precipitacion_mm REAL,
        humedad_rel_pct REAL,
        radiacion_mj_m2 REAL,
        PRIMARY KEY (id_ubicacion, fecha, fuente),
        FOREIGN KEY (id_ubicacion) REFERENCES ubicaciones(id_ubicacion)
    );

    CREATE TABLE IF NOT EXISTS pronostico_estacional (
        id_ubicacion TEXT NOT NULL,
        fecha_emision DATE NOT NULL,
        fecha_objetivo DATE NOT NULL,
        modelo TEXT NOT NULL,
        temp_media_pred_c REAL,
        precip_pred_mm REAL,
        PRIMARY KEY (id_ubicacion, fecha_emision, fecha_objetivo, modelo)
    );
    """)
    conn.commit()
    conn.close()
    print(f"[DB] Base de datos inicializada en {DB_PATH}")


if __name__ == "__main__":
    inicializar_db()
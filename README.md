# `svc-weatherForecast-toka`

Microservicio de pronóstico meteorológico híbrido y análisis climático futuro por **polígonos y coordenadas específicas (`lat`, `lon`)**, diseñado para operar en el **`PORT 5000`** dentro de la arquitectura de microservicios **toka**.

Combina modelos numéricos atmosféricos (Open-Meteo NWP / ECMWF SEAS5), reanálisis satelital histórico de la NASA (**NASA POWER 1991–2020**) y modelos de *Machine Learning* (**XGBoost / Random Forest**) calibrados con la elevación real del terreno (DEM) de cada vértice consultado.

---

## Arquitectura e Integración en `toka`

* **Puerto expuesto:** `5000`
* **Consumidor principal:** `svc-dataIntegration-toka` (`PORT 6000`)
* **Simetría de contrato:** Mantiene la misma estructura de petición `POST` con arreglo de vértices (`poligono`) utilizada por `svc-weatherHistory-toka` (`PORT 5001`), permitiendo consultas paralelas desde el orquestador.
* **Optimización espacial y caché en memoria:**
  * **Climatología base (1991–2020):** Caché persistente en memoria por celda de $0.01^\circ \times 0.01^\circ$ ($\approx 1.1\text{ km}$). Cuando se envía un polígono de parcela (ej. 4 vértices cercanos), el primer vértice descarga la climatología y los vértices subsecuentes se resuelven en microsegundos desde RAM.
  * **Pronósticos:** Caché con TTL de `3600` segundos (1 hora) para garantizar baja latencia y evitar límites de tasa (*rate-limits*) ante múltiples consultas concurrentes.

---

## Instalación y Despliegue

### 1. Despliegue con Docker (Recomendado para Producción / Integración)

El repositorio incluye el artefacto preentrenado `models/toka_forecast_bundle.joblib`, por lo que el contenedor inicia de inmediato sin requerir reentrenamiento.

```bash
# Construir imagen
docker build -t svc-weatherforecast-toka .

# Levantar contenedor en el puerto 5000
docker run -d \
  --name svc-weatherforecast-toka \
  -p 5000:5000 \
  --restart unless-stopped \
  svc-weatherforecast-toka
```

### 2. Ejecución Local (Desarrollo)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn src.main:app --host 0.0.0.0 --port 5000
```

---

## Contrato de API (Llamadas al Sistema)

### 1. Health Check
Verifica que el servicio y los modelos en memoria estén operativos.

* **Petición:**
  ```http
  GET /health HTTP/1.1
  Host: svc-weatherforecast-toka:5000
  ```
* **Respuesta (`200 OK`):**
  ```json
  {
    "status": "ok",
    "service": "svc-weatherForecast-toka",
    "mode": "polygon_and_point",
    "port": 5000,
    "models_loaded": true
  }
  ```

---

### 2. Pronóstico por Polígono de Coordenadas (Endpoint Principal)
Recibe un arreglo de coordenadas (`poligono`, de 1 a 50 vértices) y devuelve un arreglo estructurado con el pronóstico híbrido a **7 días** (con intervalos de confianza) y las anomalías estacionales a **6 meses** para cada vértice.

* **Endpoints disponibles (ambos aceptan el mismo payload):**
  * `POST /api/v1/forecast`
  * `POST /api/v1/clima-futuro` *(alias simétrico a `/api/v1/clima-historico`)*

* **Payload de Entrada (`application/json`):**
  ```json
  {
    "poligono": [
      { "lat": 20.9215, "lon": -101.4230 },
      { "lat": 20.9230, "lon": -101.4210 },
      { "lat": 20.9205, "lon": -101.4195 },
      { "lat": 20.9195, "lon": -101.4220 }
    ]
  }
  ```

* **Ejemplo de llamada (`cURL`):**
  ```bash
  curl -X POST "http://localhost:5000/api/v1/forecast" \
    -H "Content-Type: application/json" \
    -d '{
      "poligono": [
        { "lat": 20.9215, "lon": -101.4230 },
        { "lat": 20.9230, "lon": -101.4210 },
        { "lat": 20.9205, "lon": -101.4195 },
        { "lat": 20.9195, "lon": -101.4220 }
      ]
    }'
  ```

* **Ejemplo de llamada desde `svc-dataIntegration-toka` (Python / `httpx`):**
  ```python
  import httpx

  async def fetch_polygon_forecast(poligono: list[dict]) -> dict:
      url = "http://svc-weatherforecast-toka:5000/api/v1/forecast"
      async with httpx.AsyncClient(timeout=25.0) as client:
          response = await client.post(url, json={"poligono": poligono})
          response.raise_for_status()
          return response.json()
  ```

* **Estructura del JSON de Respuesta (`200 OK`):**
  ```json
  {
    "service": "svc-weatherForecast-toka",
    "timestamp_utc": "2026-11-15T15:26:09.103788+00:00",
    "total_vertices": 4,
    "resultados": [
      {
        "vertice": 1,
        "service": "svc-weatherForecast-toka",
        "timestamp_utc": "2026-11-15T15:26:09.100981+00:00",
        "ubicacion": {
          "latitud": 20.9215,
          "longitud": -101.423,
          "elevacion_dem_m": 1767.0,
          "zona_horaria": "America/Mexico_City",
          "climatologia_ref": "NASA_POWER_1991_2020"
        },
        "metricas_modelo": {
          "temp_mae_c": 0.742,
          "precip_mae_mm": 1.786
        },
        "pronostico_7_dias": [
          {
            "fecha": "2026-11-15",
            "temp_media_c": 16.87,
            "temp_min_c": 10.6,
            "temp_max_c": 24.0,
            "anomalia_temp_c": 2.39,
            "incertidumbre_temp_c": [15.27, 18.47],
            "precipitacion_mm": 0.1,
            "probabilidad_lluvia_pct": 0.0,
            "humedad_rel_pct": 53.0
          }
        ],
        "clima_futuro_estacional": [
          {
            "periodo": "2026-12",
            "temp_normal_c": 13.06,
            "temp_pronostico_c": 15.8,
            "anomalia_temp_c": 2.74,
            "precip_normal_mm": 5.1,
            "precip_pronostico_mm": 18.2,
            "anomalia_precip_pct": 260.0,
            "escenario": "Cálido / Húmedo"
          }
        ]
      }
    ]
  }
  ```

---

### 3. Pronóstico Puntual (Retrocompatibilidad)
Consulta rápida para una sola coordenada sin envolverla en un polígono.

* **Endpoint:** `GET /api/v1/forecast?lat=20.9215&lon=-101.4230`
* **Respuesta (`200 OK`):** Devuelve directamente un objeto individual de vértice (`ForecastResponse`).

---

## Diccionario de Datos y Reglas de Interpretación

| Campo | Unidad | Descripción |
| :--- | :--- | :--- |
| `ubicacion.elevacion_dem_m` | `m s.n.m.` | Altitud real extraída del Modelo Digital de Elevación (DEM) utilizada como predictor espacial. |
| `pronostico_7_dias[].anomalia_temp_c` | `°C` | Desviación térmica respecto a la normal histórica 1991–2020 (`+` más cálido, `-` más frío). |
| `pronostico_7_dias[].incertidumbre_temp_c` | `°C` | Banda de confianza al 90% `[P05, P95]` calculada con la desviación estándar residual del modelo y el horizonte de días. |
| `clima_futuro_estacional[].anomalia_precip_pct` | `%` | Variación porcentual de lluvia esperada respecto al acumulado histórico del mes. *(Nota: en temporada seca donde `precip_normal_mm < 15 mm`, evaluar siempre junto con `precip_pronostico_mm` absoluto para evitar falsas alarmas por denominadores pequeños).* |

---

## Códigos de Estado HTTP

| Código | Significado | Causa habitual |
| :--- | :--- | :--- |
| `200` | **OK** | Pronóstico y anomalías generados correctamente. |
| `422` | **Unprocessable Entity** | JSON mal formado, arreglo `poligono` vacío/excedido (>50) o coordenadas fuera de rango (`lat` $\notin [-90, 90]$, `lon` $\notin [-180, 180]$). |
| `502` | **Bad Gateway** | Indisponibilidad temporal en los proveedores meteorológicos externos (Open-Meteo / NASA POWER). |
| `500` | **Internal Server Error** | Error interno de inferencia o validación de series temporales. |

---

## Estructura del Repositorio

```text
svc-weatherForecast-toka/
├── Dockerfile                        # Imagen ligera Python 3.12-slim + libgomp1 (XGBoost)
├── README.md                         # Documentación y contrato HTTP
├── requirements.txt                  # Dependencias fijadas
├── models/
│   └── toka_forecast_bundle.joblib   # Modelos XGBoost/RF entrenados y métricas de validación
└── src/
    ├── __init__.py
    ├── database.py                   # Esquema SQLite para ingesta histórica
    ├── recolector_fase1.py           # Recolector Open-Meteo Archive + NASA POWER
    ├── procesamiento_fase2.py        # Limpieza, homologación física y cálculo de anomalías
    ├── entrenar_modelo.py            # Entrenamiento cronológico y serialización del bundle
    ├── schemas.py                    # Contratos Pydantic (PoligonoRequest, ForecastResponse)
    └── main.py                       # Servidor FastAPI (Puerto 5000) con caché TTL
```

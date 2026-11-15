
""" class UbicacionInfo(BaseModel):
    latitud: float = Field(..., description="Latitud consultada en grados decimales")
    longitud: float = Field(..., description="Longitud consultada en grados decimales")
    elevacion_dem_m: float = Field(..., description="Elevación real del terreno (m s.n.m.) obtenida del Modelo Digital de Elevación")
    zona_horaria: str = Field(..., description="Zona horaria IANA detectada automáticamente para la coordenada")
    climatologia_ref: str = Field(..., description="Fuente de la línea base histórica (1991-2020) usada para calcular anomalías")


class PronosticoDiarioItem(BaseModel):
    fecha: str = Field(..., description="Fecha objetivo en formato YYYY-MM-DD")
    temp_media_c: float = Field(..., description="Temperatura media híbrida pronosticada (°C)")
    temp_min_c: float = Field(..., description="Temperatura mínima diaria pronosticada (°C)")
    temp_max_c: float = Field(..., description="Temperatura máxima diaria pronosticada (°C)")
    anomalia_temp_c: float = Field(..., description="Desviación térmica respecto a la normal histórica del mes (°C)")
    incertidumbre_temp_c: List[float] = Field(..., description="Intervalo de confianza al 90% [P05, P95] en °C")
    precipitacion_mm: float = Field(..., description="Precipitación acumulada diaria híbrida (mm)")
    probabilidad_lluvia_pct: float = Field(..., description="Probabilidad máxima de precipitación (%)")
    humedad_rel_pct: float = Field(..., description="Humedad relativa media diaria (%)")


class AnomaliaEstacionalItem(BaseModel):
    periodo: str = Field(..., description="Mes pronosticado en formato YYYY-MM")
    temp_normal_c: float = Field(..., description="Temperatura media histórica 1991-2020 para ese mes (°C)")
    temp_pronostico_c: float = Field(..., description="Temperatura media pronosticada por ensamble SEAS5 (°C)")
    anomalia_temp_c: float = Field(..., description="Anomalía térmica estacional esperada (°C)")
    precip_normal_mm: float = Field(..., description="Precipitación mensual acumulada histórica 1991-2020 (mm)")
    precip_pronostico_mm: float = Field(..., description="Precipitación mensual acumulada pronosticada (mm)")
    anomalia_precip_pct: float = Field(..., description="Anomalía porcentual de precipitación respecto a la normal (%)")
    escenario: str = Field(..., description="Clasificación climática combinada (Térmica / Hídrica)")


class ForecastResponse(BaseModel):
    service: str = "svc-weatherForecast-toka"
    timestamp_utc: str
    ubicacion: UbicacionInfo
    metricas_modelo: dict
    pronostico_7_dias: List[PronosticoDiarioItem]
    clima_futuro_estacional: List[AnomaliaEstacionalItem] """

from typing import List
from pydantic import BaseModel, Field


class CoordenadaPoligono(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0, description="Latitud del vértice")
    lon: float = Field(..., ge=-180.0, le=180.0, description="Longitud del vértice")


class PoligonoRequest(BaseModel):
    poligono: List[CoordenadaPoligono] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Arreglo de coordenadas (ej. 4 vértices de la parcela)"
    )


class PronosticoDiarioItem(BaseModel):
    fecha: str
    temp_media_c: float
    temp_min_c: float
    temp_max_c: float
    anomalia_temp_c: float
    incertidumbre_temp_c: List[float] = Field(..., description="[P05, P95] intervalo de confianza 90%")
    precipitacion_mm: float
    probabilidad_lluvia_pct: float
    humedad_rel_pct: float


class AnomaliaEstacionalItem(BaseModel):
    periodo: str
    temp_normal_c: float
    temp_pronostico_c: float
    anomalia_temp_c: float
    precip_normal_mm: float
    precip_pronostico_mm: float
    anomalia_precip_pct: float
    escenario: str


class ForecastResponse(BaseModel):
    vertice: int = 1
    service: str = "svc-weatherForecast-toka"
    timestamp_utc: str
    ubicacion: dict
    metricas_modelo: dict
    pronostico_7_dias: List[PronosticoDiarioItem]
    clima_futuro_estacional: List[AnomaliaEstacionalItem]


class PoligonoForecastResponse(BaseModel):
    service: str = "svc-weatherForecast-toka"
    timestamp_utc: str
    total_vertices: int
    resultados: List[ForecastResponse]
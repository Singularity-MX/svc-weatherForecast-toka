FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt .

RUN pip install --no-cache-dir -r requirements-runtime.txt

RUN useradd --create-home --uid 10001 appuser

COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser models/ ./models/

RUN test -s /app/models/toka_forecast_bundle.joblib

USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3)" || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "5000"]

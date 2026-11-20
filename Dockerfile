FROM python:3.12-slim

# Port the service listens on. Override at build time (--build-arg PORT=...) or at run time (-e PORT=...).
ARG PORT=5000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=${PORT}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser src/ ./src/

USER appuser

EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'], timeout=3)" || exit 1

# Shell form on purpose so ${PORT} is expanded; `exec` keeps uvicorn as PID 1 for clean shutdowns.
CMD exec uvicorn src.main:app --host 0.0.0.0 --port ${PORT}

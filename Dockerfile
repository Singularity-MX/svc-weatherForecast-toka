# ---------- build stage: this is the only place that installs anything ----------
# tip: for real deployments pin the base image by digest (python:3.12-slim@sha256:...)
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
COPY requirements-runtime.txt .
RUN /opt/venv/bin/pip install -r requirements-runtime.txt


# ---------- final stage: what actually runs ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# dependencies come ready from the build stage, nothing is installed here (no apt, no pip)
COPY --from=builder /opt/venv /opt/venv

# no package manager inside the final image, so nobody can install tools from a shell
RUN /opt/venv/bin/python -m pip uninstall -y pip setuptools wheel \
    && /usr/local/bin/python -m pip uninstall -y pip setuptools wheel

# system user: fixed uid, no home, no login shell
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser

WORKDIR /app
# code is owned by root and the app user can only read it, so the app can't rewrite its own code.
# (no .pyc gets written, see PYTHONDONTWRITEBYTECODE, so the image also works with a read-only filesystem)
COPY src/ ./src/

USER appuser

# the app listens on the port set in src/config.py (5000 by default).
# EXPOSE is only documentation, keep it matching.
EXPOSE 5000

# healthcheck reads the port from config.py so it can't get out of sync
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; from src.config import settings; urllib.request.urlopen(f'http://127.0.0.1:{settings.port}/health', timeout=3)" || exit 1

CMD ["python", "-m", "src"]

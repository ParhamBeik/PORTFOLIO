# ---- builder: compiles deps into a venv; toolchain never reaches the final image ----
FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# psycopg[binary] ships wheels, but numpy/scipy/scikit-learn/cvxpy pull in some
# sdists depending on platform, so keep build-essential here (builder only).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=83.0.0" \
    && pip install --no-cache-dir -r requirements.txt

# ---- final: runtime image only, no compiler toolchain ----
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# libpq5: psycopg's runtime shared lib. curl: HEALTHCHECK + compose healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl poppler-utils tesseract-ocr tesseract-ocr-fas \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN rm -rf /opt/venv/bin/pip* /opt/venv/lib/python*/site-packages/pip* \
    /usr/local/lib/python*/site-packages/pip* \
    /usr/local/lib/python*/site-packages/setuptools* \
    /usr/local/lib/python*/site-packages/_distutils_hack \
    /usr/local/lib/python*/site-packages/wheel* \
    && rm -f /usr/local/bin/pip*

COPY . /app/
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/staticfiles \
    && chown -R appuser:appuser /app \
    && chmod +x /app/entrypoint.sh

USER appuser

# Portable outside compose too: no apt/pip at runtime, so appuser can run everything.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health/ || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
# uvicorn workers under gunicorn: streaming SSE needs async-friendly serving,
# and a worker per process keeps one blocked stream from starving the API.
CMD ["gunicorn", "config.asgi:application", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers", "3"]

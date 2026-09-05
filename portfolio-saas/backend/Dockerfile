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
#
# poppler-utils / tesseract-ocr / tesseract-ocr-fas used to be here and are not
# reachable from any code path: OCR was stripped from the Codal pipeline (see
# the module docstring in marketdata/codal_parsers.py and the note in
# requirements.txt), pytesseract and Pillow are deliberately not installed, and
# `grep -rn 'subprocess\|os.system\|shutil.which'` over the backend returns
# nothing -- so there is no way to invoke a binary at all. Re-add them in the
# same change that re-adds the Python bindings, not before.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN rm -rf /opt/venv/bin/pip* /opt/venv/lib/python*/site-packages/pip* \
    /usr/local/lib/python*/site-packages/pip* \
    /usr/local/lib/python*/site-packages/setuptools* \
    /usr/local/lib/python*/site-packages/_distutils_hack \
    /usr/local/lib/python*/site-packages/wheel* \
    && rm -f /usr/local/bin/pip*

# The user is created BEFORE the source is copied, so `COPY --chown` can set
# ownership as it writes. A `chown -R /app` after the copy rewrites every inode
# and therefore stores a second full copy of the tree in its own layer: with the
# context at 616 MB that was 1.2 GB of a 2.76 GB image for one tree.
RUN useradd -m -u 1000 appuser

COPY --chown=appuser:appuser . /app/

# staticfiles is in .dockerignore (collectstatic writes it at runtime, and
# compose mounts a named volume over it), so the mount point has to exist and
# be writable before the process drops privileges.
RUN mkdir -p /app/staticfiles \
    && chown appuser:appuser /app/staticfiles \
    && chmod +x /app/entrypoint.sh

USER appuser

# Portable outside compose too: no apt/pip at runtime, so appuser can run everything.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health/ || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
# uvicorn workers under gunicorn: Django wraps each connection in
# ThreadSensitiveContext, so it does not serialize requests and works correctly today.
#
# --max-requests recycles a worker after N requests, with jitter so the three
# never retire together and drop the whole pool at once. UvicornWorker maps it
# to uvicorn's limit_max_requests, so it does take effect here. This is a
# memory guard, not a tuning knob: pandas/numpy/cvxpy fragment the allocator
# badly and the analytics endpoints build a fresh DataFrame per request, so RSS
# drifts up across a long-lived process until the 1536m container limit
# OOM-kills it. Recycling returns the arena to the OS on a schedule instead.
#
# --timeout is NOT a request deadline under an async worker. UvicornWorker
# feeds it to timeout_notify, so the arbiter reaps a worker whose event loop
# has stopped heartbeating -- but Django's sync views run in a thread and the
# loop keeps notifying, so a slow request rides straight through it. Capping a
# single request still needs either the edge proxy's read timeout or explicit
# middleware; this only bounds a genuinely wedged process.

CMD ["gunicorn", "config.asgi:application", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--max-requests", "1000", \
     "--max-requests-jitter", "150", \
     "--timeout", "120", \
     "--graceful-timeout", "30"]

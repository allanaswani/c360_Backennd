# syntax=docker/dockerfile:1
#
# Runtime image for the Customer 360 backend (Django 5.2 / Python 3.12).
# Mirrors the HF portfolio backend's image so it deploys the same way: build here,
# run with `--network=host` and secrets injected at runtime via `--env-file`.
#
# Two deltas from the portfolio image:
#   * PORT defaults to 9001, not 9000 — with `--network=host` this IS the host port,
#     and 9000 is already taken by the portfolio backend on the same box.
#   * libgomp1 is installed: Customer 360 pulls in LightGBM / scikit-learn (the
#     next-best-product model), whose wheels need the OpenMP runtime at import time.
#
# Secrets are NOT baked in: .env is excluded via .dockerignore and injected at run
# time. Migrations are NOT run here — they are a deliberate manual deploy step
# (see deploy/DEPLOY.md), same as the portfolio.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings \
    PORT=9001

WORKDIR /app

# libgomp1 = OpenMP runtime required by LightGBM and scikit-learn wheels. All the
# scientific wheels (numpy/pandas/scikit-learn/lightgbm) are manylinux prebuilt, so
# nothing compiles here — only this shared library is needed at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Collect static (admin / DRF browsable API) into STATIC_ROOT so WhiteNoise can
# serve it. Settings ship a throwaway default SECRET_KEY good enough to import
# during the build; the real key is injected at run time via --env-file.
RUN python manage.py collectstatic --noinput

# Run as non-root.
RUN useradd -m -u 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 9001

# Gunicorn binds ${PORT} on all interfaces. With `--network=host` at run time this
# is the host port. 3 workers × 120s timeout matches the portfolio backend.
CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT} --workers 3 --timeout 120 --access-logfile - --error-logfile -"]

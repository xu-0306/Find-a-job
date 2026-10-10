# syntax=docker/dockerfile:1
FROM python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JOB_COMPANION_DATA=/app/data

WORKDIR /app

COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --requirement /app/requirements.lock

RUN addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --gid 10001 --home /app --no-create-home app \
    && mkdir -p /app/data \
    && chown app:app /app/data

COPY --chown=app:app app /app/app

USER app
EXPOSE 8000

CMD ["python", "-m", "app", "serve", "--host", "0.0.0.0", "--port", "8000"]

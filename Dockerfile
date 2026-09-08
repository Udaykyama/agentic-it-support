# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TMPDIR=/run/neuraldesk

WORKDIR /app

RUN groupadd --gid 10001 neuraldesk \
    && useradd --uid 10001 --gid neuraldesk --no-create-home \
       --home-dir /app --shell /usr/sbin/nologin neuraldesk \
    && mkdir -p /run/neuraldesk \
    && chown neuraldesk:neuraldesk /run/neuraldesk

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY api.py alembic.ini ./
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY templates/ ./templates/
COPY static/ ./static/
COPY docker/healthcheck.py ./docker/healthcheck.py

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

# Access logging stays disabled: OAuth callback URLs contain authorization codes.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "60", "--graceful-timeout", "60", "--keep-alive", "5", "--worker-tmp-dir", "/run/neuraldesk", "--forwarded-allow-ips", "", "--error-logfile", "-", "api:create_app()"]

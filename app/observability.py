import json
import logging
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

from flask import g, request
from prometheus_client import CollectorRegistry, Counter, Histogram


class JSONFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key in (
            "request_id", "tenant_id", "job_id", "error_type", "error_code",
            "method", "route", "status", "duration_ms", "attempt",
        ):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        if record.exc_info:
            entry["error_type"] = record.exc_info[0].__name__
            # Exception messages from SDKs and database drivers may contain ticket text or credentials.
            entry["stack"] = [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(entry, ensure_ascii=True)


def configure_logging(level):
    logger = logging.getLogger("neuraldesk")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    for name in ("httpx", "httpcore", "openai", "authlib", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return logger


def install_observability(app):
    registry = CollectorRegistry()
    requests = Counter(
        "neuraldesk_http_requests_total", "HTTP requests", ["method", "route", "status"],
        registry=registry,
    )
    latency = Histogram(
        "neuraldesk_http_request_duration_seconds", "HTTP request duration",
        ["method", "route"], registry=registry,
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15),
    )
    app.extensions["metrics_registry"] = registry

    @app.before_request
    def start_request():
        g.request_id = str(uuid.uuid4())
        g.request_started = time.monotonic()

    @app.after_request
    def record_request(response):
        request_id = getattr(g, "request_id", str(uuid.uuid4()))
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if app.config["SETTINGS"].environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if not request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        route = request.url_rule.rule if request.url_rule else "unmatched"
        method = request.method if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"
        duration = time.monotonic() - getattr(g, "request_started", time.monotonic())
        requests.labels(method, route, response.status_code).inc()
        latency.labels(method, route).observe(duration)
        principal = getattr(g, "principal", None)
        app.extensions["logger"].info("http_request", extra={
            "request_id": request_id, "method": method, "route": route,
            "status": response.status_code, "duration_ms": round(duration * 1000, 2),
            "tenant_id": principal.tenant_id if principal else None,
        })
        return response

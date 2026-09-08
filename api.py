from datetime import timedelta
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request
from flask_session import Session
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from app.auth import IdentityProvider, auth, authenticate_request
from app.classifier import AIService
from app.config import Settings
from app.db import Database
from app.errors import APIError
from app.models import Job
from app.observability import configure_logging, install_observability
from app.rate_limit import enforce_limit
from app.routes import api


def create_app(settings=None, *, database=None, redis_client=None, identity=None, ai_service=None):
    settings = settings or Settings.from_env()
    authority = "\0".join((
        settings.environment, settings.oidc_issuer, settings.oidc_client_id,
        settings.oidc_audience, settings.oidc_tenant_claim, settings.oidc_roles_claim,
    ))
    session_namespace = hmac.new(
        settings.secret_key.encode("utf-8"), authority.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    app = Flask(__name__)
    app.config.update(
        SETTINGS=settings, SECRET_KEY=settings.secret_key,
        MAX_CONTENT_LENGTH=32768, MAX_FORM_MEMORY_SIZE=32768,
        SESSION_TYPE="redis", SESSION_KEY_PREFIX=f"neuraldesk:session:{session_namespace}:",
        SESSION_COOKIE_NAME="__Host-neuraldesk" if settings.environment == "production" else "neuraldesk",
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=settings.environment == "production",
        SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_PATH="/",
        SESSION_PERMANENT=True, SESSION_REFRESH_EACH_REQUEST=False,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=settings.session_lifetime_seconds),
        TRUSTED_HOSTS=[urlsplit(settings.public_url).hostname],
    )
    if settings.trusted_proxy_hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=settings.trusted_proxy_hops,
            x_proto=settings.trusted_proxy_hops, x_host=0, x_port=0, x_prefix=0,
        )
    app.extensions["logger"] = configure_logging(settings.log_level)
    app.extensions["database"] = database if database is not None else Database(settings)
    app.extensions["database"].validate_runtime_role()
    redis_client = redis_client if redis_client is not None else Redis.from_url(
        settings.redis_url, socket_connect_timeout=3, socket_timeout=3,
        health_check_interval=30, max_connections=50,
    )
    app.extensions["redis"] = redis_client
    app.config["SESSION_REDIS"] = redis_client
    Session(app)
    app.extensions["identity"] = identity if identity is not None else IdentityProvider(app, settings)
    app.extensions["ai"] = ai_service if ai_service is not None else AIService(settings)
    install_observability(app)
    queue_size = Gauge("neuraldesk_jobs_pending", "Queued or retrying durable jobs", registry=app.extensions["metrics_registry"])
    failed_jobs = Gauge("neuraldesk_jobs_failed", "Terminally failed durable jobs", registry=app.extensions["metrics_registry"])

    @app.before_request
    def admission_control():
        if request.path.startswith(("/health/", "/static/")):
            return
        enforce_limit(redis_client, "ip", request.remote_addr or "unknown", settings.rate_limit_per_minute * 5)
        if request.path.startswith("/api/v1/") and request.path != "/api/v1/openapi.json":
            authenticate_request()

    def error_response(error):
        request_id = getattr(g, "request_id", None)
        body = {"code": error.code, "message": error.message, "request_id": request_id}
        if error.details is not None:
            body["details"] = error.details
        response = jsonify({"error": body})
        response.status_code = error.status
        if error.status == 401:
            response.headers["WWW-Authenticate"] = 'Bearer realm="neuraldesk"'
        if error.status == 429 and isinstance(error.details, dict):
            response.headers["Retry-After"] = str(error.details["retry_after"])
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        app.extensions["logger"].warning("request_rejected", extra={
            "request_id": getattr(g, "request_id", None), "error_code": error.code, "status": error.status,
        })
        if request.path.startswith("/auth/") and request.accept_mimetypes.best == "text/html":
            return render_template("auth_error.html", error={
                "code": error.code, "message": error.message, "request_id": getattr(g, "request_id", None),
            }), error.status
        return error_response(error)

    @app.errorhandler(HTTPException)
    def http_error(error):
        return error_response(APIError(error.code, "invalid_request", error.description))

    @app.errorhandler(SQLAlchemyError)
    @app.errorhandler(RedisError)
    def dependency_error(error):
        app.extensions["logger"].exception("dependency_unavailable", extra={
            "request_id": getattr(g, "request_id", None),
        })
        return error_response(APIError(503, "dependency_unavailable", "A required service is unavailable. Retry shortly."))

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.extensions["logger"].exception("unexpected_request_error", extra={
            "request_id": getattr(g, "request_id", None),
        })
        return error_response(APIError(500, "internal_error", "The request could not be completed. Use the request ID when contacting support."))

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/health/live")
    def live():
        return {"status": "live"}

    @app.get("/health/ready")
    def ready():
        app.extensions["database"].ping()
        redis_client.ping()
        return {"status": "ready"}

    @app.get("/metrics")
    def metrics():
        supplied = request.headers.get("Authorization", "").encode("utf-8")
        if not secrets.compare_digest(supplied, f"Bearer {settings.metrics_token}".encode("utf-8")):
            raise APIError(401, "authentication_required", "Supply the monitoring bearer token.")
        with app.extensions["database"].session() as db:
            queue_size.set(db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(("queued", "retrying")))))
            failed_jobs.set(db.scalar(select(func.count()).select_from(Job).where(Job.status == "failed")))
        return app.response_class(generate_latest(app.extensions["metrics_registry"]), content_type=CONTENT_TYPE_LATEST)

    @app.get("/api/v1/openapi.json")
    def openapi():
        from app.openapi import specification

        return specification(app.config["SESSION_COOKIE_NAME"])

    @app.route("/ticket", methods=["POST"])
    @app.route("/tickets", methods=["GET"])
    @app.route("/stats", methods=["GET"])
    @app.route("/runbooks", methods=["GET"])
    @app.route("/generate-runbooks", methods=["GET", "POST"])
    def legacy_endpoint():
        response = error_response(APIError(
            410, "api_upgraded", "Use the authenticated /api/v1 API. Ticket processing is now asynchronous.",
        ))
        response.headers["Link"] = '</api/v1/openapi.json>; rel="describedby"'
        return response

    app.register_blueprint(auth)
    app.register_blueprint(api)
    from app.cli import register_commands

    register_commands(app)
    if settings.environment == "development":
        app.extensions["logger"].warning("development_configuration_not_for_production")
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8000, debug=False)

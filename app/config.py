from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def secret(env, name, default=""):
    value, filename = env.get(name), env.get(f"{name}_FILE")
    if value and filename:
        raise ValueError(f"Set {name} or {name}_FILE, not both")
    if filename:
        value = Path(filename).read_text(encoding="utf-8").strip()
    return value if value is not None else default


def boolean(env, name, default):
    value = env.get(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def number(env, name, default, minimum, maximum, cast=int):
    try:
        value = cast(env.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    secret_key: str = field(repr=False)
    public_url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str = field(repr=False)
    oidc_audience: str
    oidc_tenant_claim: str
    oidc_roles_claim: str
    metrics_token: str = field(repr=False)
    ai_enabled: bool
    openai_api_key: str = field(repr=False)
    openai_base_url: str
    classification_model: str
    embedding_model: str
    ai_timeout_seconds: int
    confidence_threshold: float
    similarity_threshold: float
    session_lifetime_seconds: int
    job_lease_seconds: int
    job_max_attempts: int
    worker_poll_seconds: float
    rate_limit_per_minute: int
    ai_rate_limit_per_minute: int
    database_pool_size: int
    log_level: str
    trusted_proxy_hops: int

    @classmethod
    def from_env(cls, env=None):
        if env is None:
            load_dotenv(override=False)
            env = os.environ
        environment = env.get("APP_ENV", "production")
        if environment not in {"production", "development", "test"}:
            raise ValueError("APP_ENV must be production, development, or test")
        settings = cls(
            environment=environment,
            database_url=secret(env, "DATABASE_URL"),
            redis_url=secret(env, "REDIS_URL"),
            secret_key=secret(env, "SECRET_KEY"),
            public_url=env.get("PUBLIC_URL", "http://localhost:8000").rstrip("/"),
            oidc_issuer=env.get("OIDC_ISSUER", ""),
            oidc_client_id=env.get("OIDC_CLIENT_ID", ""),
            oidc_client_secret=secret(env, "OIDC_CLIENT_SECRET"),
            oidc_audience=env.get("OIDC_AUDIENCE", ""),
            oidc_tenant_claim=env.get("OIDC_TENANT_CLAIM", "tenant_id"),
            oidc_roles_claim=env.get("OIDC_ROLES_CLAIM", "roles"),
            metrics_token=secret(env, "METRICS_TOKEN"),
            ai_enabled=boolean(env, "AI_ENABLED", False),
            openai_api_key=secret(env, "OPENAI_API_KEY"),
            openai_base_url=env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            classification_model=env.get("CLASSIFICATION_MODEL", "gpt-4o-mini"),
            embedding_model=env.get("EMBEDDING_MODEL", "text-embedding-3-small"),
            ai_timeout_seconds=number(env, "AI_TIMEOUT_SECONDS", 40, 1, 120),
            confidence_threshold=number(env, "CONFIDENCE_THRESHOLD", 0.75, 0, 1, float),
            similarity_threshold=number(env, "RUNBOOK_SIMILARITY_THRESHOLD", 0.80, 0, 1, float),
            session_lifetime_seconds=number(env, "SESSION_LIFETIME_SECONDS", 3600, 60, 28800),
            job_lease_seconds=number(env, "JOB_LEASE_SECONDS", 180, 30, 900),
            job_max_attempts=number(env, "JOB_MAX_ATTEMPTS", 3, 1, 10),
            worker_poll_seconds=number(env, "WORKER_POLL_SECONDS", 2, 0.1, 60, float),
            rate_limit_per_minute=number(env, "RATE_LIMIT_PER_MINUTE", 120, 1, 10000),
            ai_rate_limit_per_minute=number(env, "AI_RATE_LIMIT_PER_MINUTE", 10, 1, 1000),
            database_pool_size=number(env, "DATABASE_POOL_SIZE", 5, 1, 50),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            trusted_proxy_hops=number(env, "TRUSTED_PROXY_HOPS", 0, 0, 3),
        )
        settings.validate()
        return settings

    def validate(self):
        if not self.database_url.startswith("postgresql+psycopg://"):
            if self.environment != "test" or not self.database_url.startswith("sqlite"):
                raise ValueError("DATABASE_URL must use postgresql+psycopg://")
        if not self.redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must use redis:// or rediss://")
        if len(self.secret_key) < 32 or self.secret_key.lower().startswith("change"):
            raise ValueError("SECRET_KEY must be a randomly generated secret of at least 32 characters")
        if len(self.metrics_token) < 32:
            raise ValueError("METRICS_TOKEN must contain at least 32 characters")
        for name, value in (
            ("PUBLIC_URL", self.public_url),
            ("OIDC_ISSUER", self.oidc_issuer),
            ("OPENAI_BASE_URL", self.openai_base_url),
        ):
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
            ):
                raise ValueError(f"{name} must be an absolute HTTP(S) URL without credentials or query")
            if self.environment == "production" and parsed.scheme != "https":
                raise ValueError(f"{name} must use HTTPS in production")
        if urlsplit(self.public_url).path not in {"", "/"}:
            raise ValueError("PUBLIC_URL must not include a path")
        for name, value in (
            ("OIDC_CLIENT_ID", self.oidc_client_id),
            ("OIDC_CLIENT_SECRET", self.oidc_client_secret),
            ("OIDC_AUDIENCE", self.oidc_audience),
            ("OIDC_TENANT_CLAIM", self.oidc_tenant_claim),
            ("OIDC_ROLES_CLAIM", self.oidc_roles_claim),
        ):
            if not value:
                raise ValueError(f"{name} is required")
        if self.ai_enabled and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when AI_ENABLED=true")
        if self.job_lease_seconds < self.ai_timeout_seconds * 2 + 30:
            raise ValueError("JOB_LEASE_SECONDS must allow two AI timeouts plus 30 seconds")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be a standard logging level")

from contextlib import contextmanager

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.errors import APIError
from app.models import AuditEvent, Tenant


class Database:
    def __init__(self, settings):
        options = {"pool_pre_ping": True, "hide_parameters": True}
        if settings.database_url.startswith("sqlite"):
            options.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
        else:
            options.update(
                pool_size=settings.database_pool_size, max_overflow=5, pool_timeout=5,
                pool_recycle=1800,
                connect_args={
                    "connect_timeout": 5,
                    "options": "-c statement_timeout=15000 -c lock_timeout=5000",
                },
            )
        self.engine = create_engine(settings.database_url, **options)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self, tenant_id=None):
        with self.sessions.begin() as session:
            if tenant_id is not None:
                self.bind_tenant(session, tenant_id)
            yield session

    def bind_tenant(self, session, tenant_id):
        if self.engine.dialect.name == "postgresql":
            session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
        session.info["tenant_id"] = tenant_id

    def validate_runtime_role(self):
        if self.engine.dialect.name != "postgresql":
            return
        with self.engine.connect() as connection:
            privileged = connection.scalar(text(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ))
            owner = connection.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename IN ('tickets', 'runbooks', 'audit_events') AND tableowner = current_user)"
            ))
            if privileged or owner:
                raise ValueError("DATABASE_URL must use a non-owner role without SUPERUSER or BYPASSRLS")
            protected = connection.scalar(text(
                "SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname IN ('tickets', 'runbooks', 'audit_events') "
                "AND c.relrowsecurity AND c.relforcerowsecurity"
            ))
            if protected != 3:
                raise ValueError("Run migrations before startup; tenant row-level security must be enabled and forced")

    def tenant(self, tenant_id):
        with self.session() as session:
            tenant = session.scalar(select(Tenant).where(Tenant.id == tenant_id, Tenant.active.is_(True)))
            if tenant is None:
                raise APIError(403, "tenant_not_provisioned", "Your company is not enabled for this application.")
            return tenant

    def ping(self):
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))


def audit(session, tenant_id, actor, action, resource_id, request_id, **details):
    session.add(AuditEvent(
        tenant_id=tenant_id, actor=actor, action=action, resource_id=resource_id,
        request_id=request_id, details=details,
    ))

import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock

from redis import Redis

from api import create_app
from app.auth import Principal
from app.config import Settings
from app.db import Database
from app.errors import APIError
from app.ingest import Classification, RunbookDraft
from app.models import Base, Runbook, Tenant, Ticket, new_id, utcnow

ENV = {
    "APP_ENV": "test",
    "DATABASE_URL": "sqlite+pysqlite:///:memory:",
    "REDIS_URL": "redis://localhost:6379/15",
    "SECRET_KEY": "test-secret-not-for-deployment-000000000000",
    "METRICS_TOKEN": "test-monitor-not-for-deployment-00000000000",
    "PUBLIC_URL": "http://localhost:8000",
    "OIDC_ISSUER": "https://identity.example",
    "OIDC_CLIENT_ID": "neuraldesk-browser",
    "OIDC_CLIENT_SECRET": "test-only-client-secret",
    "OIDC_AUDIENCE": "neuraldesk-api",
    "AI_ENABLED": "true",
    "OPENAI_API_KEY": "test-only-never-sent",
    "LOG_LEVEL": "CRITICAL",
}

PAYLOAD = {
    "title": "VPN connection times out",
    "description": "VPN times out on both home Wi-Fi and Ethernet.",
    "submitter": "alice@company.example",
}


class MemoryRedis(Redis):
    """In-process test double; production always uses the actual shared Redis service."""

    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()

    def __repr__(self):
        return "MemoryRedis(test-only)"

    def get(self, name):
        with self.lock:
            value, expiry = self.values.get(name, (None, None))
            if expiry is not None and expiry <= time.time():
                self.values.pop(name, None)
                return None
            return value

    def set(self, name, value, ex=None, **kwargs):
        with self.lock:
            self.values[name] = (value, time.time() + ex if ex else None)
            return True

    def delete(self, *names):
        with self.lock:
            return sum(self.values.pop(name, None) is not None for name in names)

    def exists(self, name):
        return int(self.get(name) is not None)

    def ping(self):
        return True

    def eval(self, script, numkeys, key):
        with self.lock:
            value = self.get(key)
            count = int(value) + 1 if value is not None else 1
            expiry = self.values[key][1] if value is not None else time.time() + 60
            self.values[key] = (count, expiry)
            return [count, max(0, int(expiry - time.time()))]

    def close(self):
        return None


class FakeIdentity:
    def verify(self, token):
        parts = token.split(":")
        if len(parts) != 3 or parts[0] not in {"company-a", "company-b"}:
            raise APIError(401, "invalid_token", "Invalid test token.")
        tenant, role, subject = parts
        return Principal(subject, tenant, (role,), int(time.time()) + 3600, f"{subject}@company.example")


def fake_ai():
    service = Mock()
    service.classify.return_value = Classification(category="network", subcategory="vpn_timeout", confidence=0.9)
    service.embed.return_value = [1.0] + [0.0] * 1535
    service.draft_runbook.return_value = RunbookDraft(
        title="VPN timeout investigation", problem="Recurring VPN timeouts",
        root_cause="Hypothesis: connectivity interruption; verify before changing configuration.",
        steps="1. Confirm the error and check service status.\n2. Escalate if connectivity is still unavailable.",
        prevention="Review recurring incidents with the network team.",
    )
    return service


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.from_env(ENV)
        self.database = Database(self.settings)
        Base.metadata.create_all(self.database.engine)
        self.redis = MemoryRedis()
        self.ai = fake_ai()
        self.app = self.make_app()
        self.client = self.app.test_client()
        with self.database.session() as db:
            db.add_all([
                Tenant(id="company-a", name="Company A", escalation_target="helpdesk@a.example"),
                Tenant(id="company-b", name="Company B", escalation_target="helpdesk@b.example"),
            ])

    def tearDown(self):
        self.database.engine.dispose()

    def make_app(self, **changes):
        settings = replace(self.settings, **changes) if changes else self.settings
        return create_app(
            settings, database=self.database, redis_client=self.redis,
            identity=FakeIdentity(), ai_service=self.ai,
        )

    def headers(self, tenant="company-a", role="agent", subject="alice", **extra):
        return {"Authorization": f"Bearer {tenant}:{role}:{subject}", **extra}

    def submit(self, **kwargs):
        return self.client.post(
            "/api/v1/tickets", json=kwargs.pop("json", PAYLOAD),
            headers=kwargs.pop("headers", self.headers()), **kwargs,
        )

    def seed_ticket(self, tenant="company-a", **values):
        row = {
            "id": new_id(), "tenant_id": tenant, "created_by": "alice",
            **PAYLOAD, "status": "escalated", "category": "network", "subcategory": "vpn_timeout",
            "confidence": 0.9, "request_hash": "seed", "created_at": utcnow(), **values,
        }
        with self.database.session(tenant) as db:
            ticket = Ticket(**row)
            db.add(ticket)
            db.flush()
            return ticket

    def seed_runbook(self, tenant="company-a", **values):
        row = {
            "id": new_id(), "tenant_id": tenant, "category": "network", "subcategory": "vpn_timeout",
            "title": "VPN investigation", "problem": "Timeout", "root_cause": "Unverified hypothesis",
            "steps": "Verify connectivity, then escalate if needed.", "prevention": "Monitor service health.",
            "source_fingerprint": new_id(), "source_ticket_ids": [], "status": "draft", **values,
        }
        with self.database.session(tenant) as db:
            book = Runbook(**row)
            db.add(book)
            db.flush()
            return book

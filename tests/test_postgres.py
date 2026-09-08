import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

from redis import Redis
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from api import create_app
from app.auth import Principal
from app.config import Settings
from app.db import Database
from app.jobs import JobProcessor, claim_next, enqueue
from app.models import AuditEvent, Job, Runbook, Tenant, Ticket, new_id, utcnow
from app.routes import duplicate_ticket
from app.runbook_kb import search_runbook
from tests.helpers import ENV, PAYLOAD, fake_ai


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL") and os.getenv("TEST_REDIS_URL"),
    "Set TEST_DATABASE_URL and TEST_REDIS_URL to exercise PostgreSQL/pgvector and real Redis",
)
class PostgreSQLIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.from_env({
            **ENV, "DATABASE_URL": os.environ["TEST_DATABASE_URL"], "REDIS_URL": os.environ["TEST_REDIS_URL"],
        })
        self.database = Database(self.settings)
        if not self.database.engine.url.database.endswith("_test"):
            self.database.engine.dispose()
            raise RuntimeError("Integration coverage requires an isolated database whose name ends with _test.")
        self.database.validate_runtime_role()
        self.other_database = Database(self.settings)
        self.redis = Redis.from_url(self.settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
        self.redis.ping()
        self.tenant_a, self.tenant_b = f"test-{new_id()}", f"test-{new_id()}"
        self.actor = f"test-{new_id()}"
        self.ai = fake_ai()
        self.identity = Mock()
        self.identity.verify.side_effect = lambda token: Principal(
            self.actor, self.tenant_b if token == "b" else self.tenant_a, ("admin",),
            int(time.time()) + 3600, PAYLOAD["submitter"],
        )
        with self.database.session() as db:
            db.add_all([
                Tenant(id=self.tenant_a, name="Isolated integration A", escalation_target="a@example.com"),
                Tenant(id=self.tenant_b, name="Isolated integration B", escalation_target="b@example.com"),
            ])
        self.app = self.make_app(self.database)
        self.replica = self.make_app(self.other_database)

    def make_app(self, database, **changes):
        return create_app(
            replace(self.settings, **changes), database=database, redis_client=self.redis,
            identity=self.identity, ai_service=self.ai,
        )

    def tearDown(self):
        for tenant in (self.tenant_a, self.tenant_b):
            with self.database.session(tenant) as db:
                for model in (AuditEvent, Ticket, Runbook, Job):
                    db.execute(delete(model).where(model.tenant_id == tenant))
                db.execute(delete(Tenant).where(Tenant.id == tenant))
        self.database.engine.dispose()
        self.other_database.engine.dispose()
        self.redis.close()

    def seed_ticket(self, tenant, **changes):
        with self.database.session(tenant) as db:
            ticket = Ticket(
                id=new_id(), tenant_id=tenant, created_by=self.actor, **PAYLOAD,
                request_hash="test", **changes,
            )
            db.add(ticket)
            db.flush()
            return ticket

    def seed_book(self, tenant, status="approved", vector=None):
        with self.database.session(tenant) as db:
            book = Runbook(
                id=new_id(), tenant_id=tenant, category="network", subcategory="vpn_timeout",
                title="Approved connectivity procedure", problem="VPN timeout", root_cause="Unverified",
                steps="Inspect service status and escalate if needed.", prevention="Monitor the service.",
                status=status, embedding=vector, source_ticket_ids=[], source_fingerprint=new_id(),
            )
            db.add(book)
            db.flush()
            return book

    def test_rls_blocks_unscoped_reads_and_cross_company_writes(self):
        a = self.seed_ticket(self.tenant_a)
        self.seed_ticket(self.tenant_b)
        with self.database.session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Ticket)), 0)
        with self.database.session(self.tenant_a) as db:
            self.assertEqual(db.scalars(select(Ticket.id)).all(), [a.id])
        with self.assertRaises(DBAPIError):
            with self.database.session(self.tenant_a) as db:
                db.add(Ticket(
                    tenant_id=self.tenant_b, created_by=self.actor, **PAYLOAD, request_hash="wrong-company",
                ))
                db.flush()
        with self.database.session() as db:
            self.assertFalse(db.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")))
            self.assertEqual(db.scalar(text(
                "SELECT COUNT(*) FROM pg_class WHERE relname IN ('tickets', 'runbooks', 'audit_events') "
                "AND relrowsecurity AND relforcerowsecurity"
            )), 3)

    def test_vector_lookup_filters_company_approval_category_and_distance(self):
        vector = [1.0] + [0.0] * 1535
        approved = self.seed_book(self.tenant_a, vector=vector)
        self.seed_book(self.tenant_a, status="draft", vector=vector)
        self.seed_book(self.tenant_b, vector=vector)
        with self.database.session(self.tenant_a) as db:
            self.assertEqual(search_runbook(db, self.tenant_a, "network", vector, 0.99).id, approved.id)
            self.assertIsNone(search_runbook(db, self.tenant_a, "hardware", vector, 0.8))
            self.assertIsNone(search_runbook(db, self.tenant_a, "network", [0.0, 1.0] + [0.0] * 1534, 0.8))
        with self.assertRaises(IntegrityError):
            other = self.seed_book(self.tenant_b, vector=vector)
            self.seed_ticket(self.tenant_a, runbook_id=other.id)

    def test_competing_workers_claim_once_and_expired_lease_is_fenced(self):
        ticket = self.seed_ticket(self.tenant_a)
        with self.database.session(self.tenant_a) as db:
            enqueue(db, self.tenant_a, "classify_ticket", self.actor, new_id(), ticket.id)
        barrier = threading.Barrier(2)

        def claim(database):
            barrier.wait(timeout=5)
            return claim_next(database, self.settings)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim, db) for db in (self.database, self.other_database)]
            claims = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(job is not None for job in claims), 1)
        original = next(job for job in claims if job)
        with self.database.session() as db:
            db.get(Job, original.id).lease_expires_at = utcnow() - timedelta(seconds=1)
        replacement = claim_next(self.other_database, self.settings)
        processor = JobProcessor(self.database, self.settings, self.ai, Mock())
        processor.process(original)
        self.ai.classify.assert_not_called()
        processor.process(replacement)
        with self.database.session(self.tenant_a) as db:
            self.assertEqual(db.get(Ticket, ticket.id).status, "escalated")

    def test_simultaneous_replica_submission_is_idempotent(self):
        barrier = threading.Barrier(2)
        key = new_id()

        def synchronized_lookup(db, idempotency_key, fingerprint):
            result = duplicate_ticket(db, idempotency_key, fingerprint)
            if result is None:
                barrier.wait(timeout=5)
            return result

        def submit(app):
            return app.test_client().post(
                "/api/v1/tickets", json=PAYLOAD,
                headers={"Authorization": "Bearer a", "Idempotency-Key": key},
            )

        with patch("app.routes.duplicate_ticket", side_effect=synchronized_lookup):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(submit, app) for app in (self.app, self.replica)]
                results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sorted(result.status_code for result in results), [200, 202])
        self.assertEqual(results[0].json["ticket"]["id"], results[1].json["ticket"]["id"])
        with self.database.session(self.tenant_a) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Ticket)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(Job).where(Job.tenant_id == self.tenant_a)), 1)

    def test_real_redis_limits_and_sessions_are_shared_between_replicas(self):
        first = self.make_app(self.database, rate_limit_per_minute=2).test_client()
        second = self.make_app(self.other_database, rate_limit_per_minute=2).test_client()
        headers = {"Authorization": "Bearer a"}
        self.assertEqual(first.get("/api/v1/me", headers=headers).status_code, 200)
        self.assertEqual(second.get("/api/v1/me", headers=headers).status_code, 200)
        self.assertEqual(first.get("/api/v1/me", headers=headers).status_code, 429)
        browser_a, browser_b = self.app.test_client(), self.replica.test_client()
        with browser_a.session_transaction() as session:
            session["principal"] = Principal(
                f"browser-{new_id()}", self.tenant_a, ("agent",), int(time.time()) + 3600, PAYLOAD["submitter"],
            ).to_dict()
            session["csrf_token"] = "shared-browser-csrf"
        browser_b.set_cookie("neuraldesk", browser_a.get_cookie("neuraldesk").value)
        me = browser_b.get("/api/v1/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json["csrf_token"], "shared-browser-csrf")
        self.assertEqual(browser_b.post("/api/v1/tickets", json=PAYLOAD).status_code, 403)
        self.assertEqual(browser_b.post(
            "/api/v1/tickets", json=PAYLOAD, headers={"X-CSRF-Token": "shared-browser-csrf"},
        ).status_code, 202)

import time
from unittest.mock import patch

from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select

from app.auth import Principal
from app.models import AuditEvent, Job, Ticket
from tests.helpers import AppTestCase, PAYLOAD


class APITests(AppTestCase):
    def test_all_business_endpoints_require_identity(self):
        for route in ("me", "tickets", "runbooks", "stats", "insights", "audit"):
            with self.subTest(route=route):
                result = self.client.get(f"/api/v1/{route}")
                self.assertEqual(result.status_code, 401)
                self.assertEqual(result.json["error"]["code"], "authentication_required")
                self.assertIn("X-Request-ID", result.headers)

    def test_invalid_input_is_rejected_before_persistence(self):
        cases = [
            None, [], "not an object", {}, {**PAYLOAD, "title": "  "},
            {**PAYLOAD, "title": 12}, {**PAYLOAD, "description": ["bad"]},
            {**PAYLOAD, "submitter": "not-an-email"}, {**PAYLOAD, "title": "a" * 201},
            {**PAYLOAD, "description": "a" * 8001}, {**PAYLOAD, "tenant_id": "company-b"},
            {**PAYLOAD, "category_hint": "administrator"},
        ]
        for payload in cases:
            with self.subTest(payload=type(payload).__name__):
                response = self.client.post("/api/v1/tickets", json=payload, headers=self.headers())
                self.assertIn(response.status_code, (400, 415))
                self.assertIn("error", response.json)
        with self.database.session("company-a") as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Ticket)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Job)), 0)

    def test_invalid_json_oversized_body_and_query_are_errors(self):
        malformed = self.client.post("/api/v1/tickets", data="{", content_type="application/json", headers=self.headers())
        self.assertEqual(malformed.status_code, 400)
        oversized = self.client.post("/api/v1/tickets", data="x" * 32769, content_type="application/json", headers=self.headers())
        self.assertEqual(oversized.status_code, 413)
        for query in ("limit=0", "limit=101", "offset=-1", "offset=10001", "limit=1&limit=2", "tenant_id=company-b", "status=unknown"):
            self.assertEqual(self.client.get(f"/api/v1/tickets?{query}", headers=self.headers()).status_code, 400)

    def test_atomic_submission_and_idempotency(self):
        headers = self.headers(**{"Idempotency-Key": "same-submission"})
        first = self.submit(headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json["ticket"]["status"], "pending")
        self.assertEqual(first.json["job"]["status"], "queued")
        replay = self.submit(headers=headers)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json["duplicate"])
        self.assertEqual(replay.json["ticket"]["id"], first.json["ticket"]["id"])
        conflict = self.submit(json={**PAYLOAD, "title": "Different issue"}, headers=headers)
        self.assertEqual(conflict.status_code, 409)
        with self.database.session("company-a") as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Ticket)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(Job)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(AuditEvent)), 1)
        second_replica = self.make_app().test_client()
        replay_on_replica = second_replica.post("/api/v1/tickets", json=PAYLOAD, headers=headers)
        self.assertEqual(replay_on_replica.status_code, 200)
        self.ai.classify.assert_not_called()

    def test_enqueue_failure_rolls_back_ticket(self):
        with patch("app.routes.enqueue", side_effect=RuntimeError("private diagnostic text")):
            response = self.submit()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private diagnostic", response.get_data(as_text=True))
        with self.database.session("company-a") as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Ticket)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Job)), 0)

    def test_company_and_subject_isolation(self):
        a = self.submit().json
        b = self.submit(headers=self.headers("company-b")).json
        other_subject = self.submit(headers=self.headers(subject="bob")).json
        company = self.client.get("/api/v1/tickets", headers=self.headers()).json
        self.assertEqual({item["id"] for item in company["tickets"]}, {a["ticket"]["id"], other_subject["ticket"]["id"]})
        requester = self.client.get("/api/v1/tickets", headers=self.headers(role="requester")).json
        self.assertEqual([item["id"] for item in requester["tickets"]], [a["ticket"]["id"]])
        for resource, item in (("tickets", b["ticket"]), ("jobs", b["job"])):
            self.assertEqual(self.client.get(f"/api/v1/{resource}/{item['id']}", headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/v1/tickets/{other_subject['ticket']['id']}", headers=self.headers(role="requester"),
        ).status_code, 404)
        result = self.client.get("/api/v1/stats", headers=self.headers(role="requester"))
        self.assertEqual(result.json["total"], 1)

    def test_idempotency_does_not_deduplicate_other_companies_or_subjects(self):
        ids = set()
        for tenant, subject in (("company-a", "alice"), ("company-a", "bob"), ("company-b", "alice")):
            response = self.submit(headers=self.headers(tenant, subject=subject, **{"Idempotency-Key": "external-42"}))
            self.assertEqual(response.status_code, 202)
            ids.add(response.json["ticket"]["id"])
        self.assertEqual(len(ids), 3)

    def test_roles_and_requester_email(self):
        self.assertEqual(self.submit(headers=self.headers(role="viewer")).status_code, 403)
        self.assertEqual(self.submit(
            json={**PAYLOAD, "submitter": "somebody@company.example"}, headers=self.headers(role="requester"),
        ).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/insights", headers=self.headers(role="requester")).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/audit", headers=self.headers()).status_code, 403)

    def test_combined_viewer_requester_roles_do_not_grant_company_writes(self):
        own = self.seed_ticket(status="failed")
        other = self.seed_ticket(created_by="bob", status="failed")
        principal = Principal("alice", "company-a", ("requester", "viewer"), int(time.time()) + 3600, PAYLOAD["submitter"])
        with patch.object(self.app.extensions["identity"], "verify", return_value=principal):
            read = self.client.get(f"/api/v1/tickets/{other.id}", headers=self.headers())
            self.assertEqual(read.status_code, 200)
            self.assertFalse(read.json["ticket"]["can_retry"])
            write = self.client.post(f"/api/v1/tickets/{other.id}/retry", headers=self.headers())
            self.assertEqual(write.status_code, 404)
            own_read = self.client.get(f"/api/v1/tickets/{own.id}", headers=self.headers())
            self.assertTrue(own_read.json["ticket"]["can_retry"])
            self.assertEqual(self.client.post(f"/api/v1/tickets/{own.id}/retry", headers=self.headers()).status_code, 202)
        with self.database.session("company-a") as db:
            self.assertEqual(db.get(Ticket, other.id).status, "failed")

    def test_manual_mode_is_explicit_and_queues_no_ai_work(self):
        app = self.make_app(ai_enabled=False)
        response = app.test_client().post("/api/v1/tickets", json=PAYLOAD, headers=self.headers())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["ticket"]["status"], "escalated")
        self.assertEqual(response.json["ticket"]["reason"], "ai_disabled")
        self.assertEqual(response.json["ticket"]["assigned_to"], "helpdesk@a.example")
        self.assertIsNone(response.json["job"])

    def test_confirmation_preserves_recommendation_and_is_audited(self):
        ticket = self.seed_ticket(status="recommended", recommendation="Original approved advice")
        path = f"/api/v1/tickets/{ticket.id}/resolve"
        self.assertEqual(self.client.post(path, json={"resolution": "Verified fixed"}, headers=self.headers(role="viewer")).status_code, 403)
        response = self.client.post(path, json={"resolution": "Verified fixed"}, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["ticket"]["status"], "resolved")
        self.assertEqual(response.json["ticket"]["resolution"], "Verified fixed")
        self.assertEqual(response.json["ticket"]["recommendation"], "Original approved advice")
        self.assertIsNotNone(response.json["ticket"]["resolved_at"])
        self.assertEqual(self.client.post(path, json={"resolution": "Verified fixed"}, headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.post(path, json={"resolution": "Changed history"}, headers=self.headers()).status_code, 409)
        self.assertEqual(self.client.get("/api/v1/stats", headers=self.headers()).json["resolution_rate"], 1)

    def test_runbook_visibility_approval_permissions_and_cross_tenant_access(self):
        draft = self.seed_runbook()
        approved = self.seed_runbook(status="approved", approved_by="operator", source_ticket_ids=["private-id"])
        other = self.seed_runbook("company-b")
        requester = self.client.get("/api/v1/runbooks", headers=self.headers(role="requester")).json["runbooks"]
        self.assertEqual([book["id"] for book in requester], [approved.id])
        self.assertEqual(requester[0]["source_ticket_ids"], [])
        self.assertIsNone(requester[0]["approved_by"])
        self.assertEqual(self.client.post(f"/api/v1/runbooks/{draft.id}/approve", headers=self.headers()).status_code, 403)
        self.assertEqual(self.client.post(f"/api/v1/runbooks/{other.id}/approve", headers=self.headers(role="admin")).status_code, 404)
        response = self.client.post(f"/api/v1/runbooks/{draft.id}/approve", headers=self.headers(role="admin"))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["runbook"]["status"], "approving")
        self.assertEqual(self.client.post(f"/api/v1/runbooks/{draft.id}/approve", headers=self.headers(role="admin")).status_code, 409)

    def test_cookie_auth_requires_csrf_and_expires(self):
        with self.client.session_transaction() as session:
            session["principal"] = {
                "subject": "alice", "tenant_id": "company-a", "roles": ["agent"],
                "expires_at": int(time.time()) + 3600, "email": PAYLOAD["submitter"],
            }
            session["csrf_token"] = "test-csrf"
        self.assertEqual(self.client.post("/api/v1/tickets", json=PAYLOAD).status_code, 403)
        response = self.client.post("/api/v1/tickets", json=PAYLOAD, headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.client.post("/auth/logout").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/me", headers={"Authorization": "Bearer invalid"}).status_code, 401)
        with self.client.session_transaction() as session:
            session["principal"] = {**session["principal"], "expires_at": int(time.time()) - 1}
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    def test_secret_or_identity_changes_invalidate_old_browser_sessions(self):
        with self.client.session_transaction() as session:
            session["principal"] = {
                "subject": "alice", "tenant_id": "company-a", "roles": ["admin"],
                "expires_at": int(time.time()) + 3600, "email": PAYLOAD["submitter"],
            }
            session["csrf_token"] = "original-csrf"
        cookie = self.client.get_cookie("neuraldesk").value
        for changes in (
            {"secret_key": "rotated-secret-not-for-production-00000000"},
            {"oidc_issuer": "https://replacement-identity.example"},
            {"oidc_client_id": "a-different-application"},
        ):
            with self.subTest(changes=changes):
                replica = self.make_app(**changes).test_client()
                replica.set_cookie("neuraldesk", cookie)
                self.assertEqual(replica.get("/api/v1/me").status_code, 401)
        replica = self.make_app().test_client()
        replica.set_cookie("neuraldesk", cookie)
        self.assertEqual(replica.get("/api/v1/me").status_code, 200)

    def test_shared_rate_limits_and_dependency_failure(self):
        app1 = self.make_app(rate_limit_per_minute=2).test_client()
        app2 = self.make_app(rate_limit_per_minute=2).test_client()
        self.assertEqual(app1.get("/api/v1/me", headers=self.headers()).status_code, 200)
        self.assertEqual(app2.get("/api/v1/me", headers=self.headers()).status_code, 200)
        limited = app1.get("/api/v1/me", headers=self.headers())
        self.assertEqual(limited.status_code, 429)
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)
        with patch.object(self.redis, "eval", side_effect=RedisConnectionError("private-redis-url")):
            unavailable = self.client.get("/api/v1/me", headers=self.headers())
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn("private-redis-url", unavailable.get_data(as_text=True))

    def test_health_monitoring_headers_and_legacy_routes(self):
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/health/ready").status_code, 200)
        with patch.object(self.redis, "ping", side_effect=RedisConnectionError()):
            self.assertEqual(self.client.get("/health/ready").status_code, 503)
            self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/metrics").status_code, 401)
        metrics = self.client.get("/metrics", headers={"Authorization": f"Bearer {self.settings.metrics_token}"})
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("neuraldesk_http_requests_total", metrics.get_data(as_text=True))
        self.assertEqual(metrics.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", metrics.headers)
        self.assertEqual(self.client.get("/tickets").status_code, 410)
        self.assertEqual(self.client.get("/generate-runbooks").status_code, 410)
        self.assertEqual(self.client.get("/api/v1/me", headers={**self.headers(), "Host": "attacker.example"}).status_code, 400)

    def test_pagination_and_openapi_cover_business_routes(self):
        for index in range(3):
            self.seed_ticket(title=f"Issue {index}")
        page = self.client.get("/api/v1/tickets?limit=2", headers=self.headers()).json
        self.assertEqual(len(page["tickets"]), 2)
        self.assertTrue(page["pagination"]["has_more"])
        self.assertEqual(page["pagination"]["total"], 3)
        document = self.client.get("/api/v1/openapi.json").json
        self.assertEqual(document["openapi"], "3.1.0")
        for rule in self.app.url_map.iter_rules():
            if rule.rule.startswith("/api/v1/") and rule.rule != "/api/v1/openapi.json":
                path = rule.rule.replace("<uuid:", "{").replace(">", "}")
                self.assertIn(path, document["paths"])
                for method in rule.methods - {"HEAD", "OPTIONS"}:
                    self.assertIn(method.lower(), document["paths"][path])

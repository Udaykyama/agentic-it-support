from datetime import timedelta
from unittest.mock import Mock, patch

import httpx
from openai import APIConnectionError
from sqlalchemy import func, select

from app.errors import ModelResponseError
from app.ingest import Classification
from app.jobs import JobProcessor, claim_next
from app.models import Job, Runbook, Ticket, utcnow
from tests.helpers import AppTestCase


class JobTests(AppTestCase):
    def processor(self):
        return JobProcessor(self.database, self.settings, self.ai, Mock())

    def ticket(self, identifier):
        with self.database.session("company-a") as db:
            return db.scalar(select(Ticket).where(Ticket.id == identifier, Ticket.tenant_id == "company-a"))

    def test_low_confidence_and_empty_knowledge_base_escalate(self):
        for confidence in (0.2, 0.95):
            response = self.submit().json
            self.ai.classify.return_value = Classification(category="network", subcategory="vpn_timeout", confidence=confidence)
            job = claim_next(self.database, self.settings)
            self.processor().process(job)
            ticket = self.ticket(response["ticket"]["id"])
            self.assertEqual(ticket.status, "escalated")
            self.assertEqual(ticket.assigned_to, "helpdesk@a.example")
            self.assertEqual(ticket.confidence, confidence)
        self.ai.embed.assert_not_called()

    def test_approved_match_recommends_and_does_not_claim_resolution(self):
        book = self.seed_runbook(status="approved", embedding=[1.0] + [0.0] * 1535)
        created = self.submit().json
        job = claim_next(self.database, self.settings)
        with patch("app.jobs.search_runbook", return_value=book) as search:
            self.processor().process(job)
            self.assertEqual(search.call_args.args[1], "company-a")
            self.assertEqual(search.call_args.args[-1], 0.8)
        ticket = self.ticket(created["ticket"]["id"])
        self.assertEqual(ticket.status, "recommended")
        self.assertEqual(ticket.recommendation, book.steps)
        self.assertIsNone(ticket.resolution)
        self.assertIsNone(ticket.resolved_at)
        stats = self.client.get("/api/v1/stats", headers=self.headers()).json
        self.assertEqual(stats["resolution_rate"], 0)

    def test_human_confirmation_during_ai_call_is_not_overwritten(self):
        created = self.submit().json
        job = claim_next(self.database, self.settings)

        def confirm_during_classification(ticket):
            response = self.client.post(
                f"/api/v1/tickets/{ticket.id}/resolve", json={"resolution": "Operator verified connectivity"},
                headers=self.headers(),
            )
            self.assertEqual(response.status_code, 200)
            return Classification(category="network", subcategory="vpn_timeout", confidence=0.2)

        self.ai.classify.side_effect = confirm_during_classification
        self.processor().process(job)
        ticket = self.ticket(created["ticket"]["id"])
        self.assertEqual(ticket.status, "resolved")
        self.assertEqual(ticket.resolution, "Operator verified connectivity")

    def test_transient_provider_error_retries_then_recovers(self):
        created = self.submit().json
        job = claim_next(self.database, self.settings)
        self.ai.classify.side_effect = APIConnectionError(request=httpx.Request("POST", "https://model.example"))
        self.processor().process(job)
        with self.database.session() as db:
            pending = db.get(Job, job.id)
            self.assertEqual(pending.status, "retrying")
            pending.available_at = utcnow() - timedelta(seconds=1)
        self.ai.classify.side_effect = None
        retry = claim_next(self.database, self.settings)
        self.assertEqual(retry.attempts, 2)
        self.processor().process(retry)
        self.assertEqual(self.ticket(created["ticket"]["id"]).status, "escalated")

    def test_invalid_model_output_is_explicit_failure_and_retryable_by_user(self):
        created = self.submit().json
        job = claim_next(self.database, self.settings)
        self.ai.classify.side_effect = ModelResponseError("invalid_model_response")
        self.processor().process(job)
        ticket = self.ticket(created["ticket"]["id"])
        self.assertEqual(ticket.status, "failed")
        self.assertEqual(ticket.error_code, "invalid_model_response")
        retry = self.client.post(f"/api/v1/tickets/{ticket.id}/retry", headers=self.headers(role="requester"))
        self.assertEqual(retry.status_code, 202)
        self.assertEqual(retry.json["ticket"]["status"], "pending")
        self.assertNotEqual(retry.json["job"]["id"], job.id)

    def test_lease_fencing_and_crash_recovery(self):
        created = self.submit().json
        original = claim_next(self.database, self.settings)
        with self.database.session() as db:
            db.get(Job, original.id).lease_expires_at = utcnow() - timedelta(seconds=1)
        replacement = claim_next(self.database, self.settings)
        self.assertNotEqual(original.lease_token, replacement.lease_token)
        self.processor().process(original)
        self.ai.classify.assert_not_called()
        self.processor().process(replacement)
        self.assertEqual(self.ticket(created["ticket"]["id"]).status, "escalated")

    def test_repeated_worker_crashes_eventually_fail_instead_of_sticking(self):
        created = self.submit().json
        job = claim_next(self.database, self.settings)
        with self.database.session() as db:
            stored = db.get(Job, job.id)
            stored.attempts = self.settings.job_max_attempts
            stored.lease_expires_at = utcnow() - timedelta(seconds=1)
        self.assertIsNone(claim_next(self.database, self.settings))
        ticket = self.ticket(created["ticket"]["id"])
        self.assertEqual(ticket.status, "failed")
        self.assertEqual(ticket.error_code, "worker_lease_expired")

    def generate(self):
        for _ in range(3):
            self.seed_ticket()
        result = self.client.post(
            "/api/v1/runbooks/generate", json={"category": "network", "subcategory": "vpn_timeout"},
            headers=self.headers(),
        )
        self.assertEqual(result.status_code, 202)
        return result.json["job"]

    def test_runbook_generation_is_deduplicated_tenant_scoped_and_stays_draft(self):
        job = self.generate()
        for _ in range(3):
            self.seed_ticket("company-b", title="Other company's confidential issue")
        duplicate = self.client.post(
            "/api/v1/runbooks/generate", json={"category": "network", "subcategory": "vpn_timeout"},
            headers=self.headers(),
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json["job"]["id"], job["id"])
        self.processor().process(claim_next(self.database, self.settings))
        source_records = self.ai.draft_runbook.call_args.args[2]
        self.assertTrue(all(ticket.tenant_id == "company-a" for ticket in source_records))
        with self.database.session("company-a") as db:
            book = db.scalar(select(Runbook).where(Runbook.tenant_id == "company-a"))
            self.assertEqual(book.status, "draft")
            self.assertIsNone(book.embedding)
            self.assertEqual(len(book.source_ticket_ids), 3)
        self.ai.embed.assert_not_called()

    def test_approval_requires_embedding_and_revocation_wins_in_flight(self):
        draft = self.seed_runbook()
        result = self.client.post(f"/api/v1/runbooks/{draft.id}/approve", headers=self.headers(role="admin"))
        self.assertEqual(result.status_code, 202)
        job = claim_next(self.database, self.settings)

        def revoke_during_embedding(text):
            response = self.client.post(f"/api/v1/runbooks/{draft.id}/revoke", headers=self.headers(role="admin"))
            self.assertEqual(response.status_code, 200)
            return [1.0] + [0.0] * 1535

        self.ai.embed.side_effect = revoke_during_embedding
        self.processor().process(job)
        with self.database.session("company-a") as db:
            book = db.get(Runbook, draft.id)
            self.assertEqual(book.status, "rejected")
            self.assertIsNone(book.embedding)

    def test_successful_approval_and_failed_approval_return_to_draft(self):
        approved = self.seed_runbook()
        self.client.post(f"/api/v1/runbooks/{approved.id}/approve", headers=self.headers(role="admin"))
        self.processor().process(claim_next(self.database, self.settings))
        with self.database.session("company-a") as db:
            book = db.get(Runbook, approved.id)
            self.assertEqual(book.status, "approved")
            self.assertEqual(book.approved_by, "alice")
            self.assertIsNotNone(book.approved_at)
        failed = self.seed_runbook()
        self.client.post(f"/api/v1/runbooks/{failed.id}/approve", headers=self.headers(role="admin"))
        self.ai.embed.side_effect = ModelResponseError("invalid_embedding")
        self.processor().process(claim_next(self.database, self.settings))
        with self.database.session("company-a") as db:
            book = db.get(Runbook, failed.id)
            self.assertEqual(book.status, "draft")
            self.assertEqual(book.error_code, "invalid_embedding")
            self.assertIsNone(book.approved_by)

    def test_insufficient_sources_do_not_enqueue_a_job(self):
        self.seed_ticket()
        response = self.client.post(
            "/api/v1/runbooks/generate", json={"category": "network", "subcategory": "vpn_timeout"},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 409)
        with self.database.session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Job)), 0)

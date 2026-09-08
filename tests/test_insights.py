from datetime import datetime, timedelta, timezone

from app.insights import recurring_incidents
from tests.helpers import AppTestCase


class InsightTests(AppTestCase):
    def test_adjacent_windows_scoring_and_tenant_scoping(self):
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        current_ids = []
        for days, status in ((1, "escalated"), (2, "recommended"), (3, "resolved")):
            current_ids.append(self.seed_ticket(created_at=now - timedelta(days=days), status=status).id)
        self.seed_ticket(created_at=now - timedelta(days=20))
        self.seed_ticket(created_at=now - timedelta(days=29))
        self.seed_ticket(created_at=now + timedelta(days=1))
        self.seed_ticket(created_at=now - timedelta(days=1), category=None, subcategory=None)
        for _ in range(8):
            self.seed_ticket("company-b", created_at=now - timedelta(days=1))
        self.seed_runbook("company-b", status="approved")
        with self.database.session("company-a") as db:
            result = recurring_incidents(db, "company-a", now=now)
        self.assertEqual(len(result["recurring_issues"]), 1)
        issue = result["recurring_issues"][0]
        self.assertEqual(issue["ticket_count"], 3)
        self.assertEqual(issue["previous_count"], 1)
        self.assertEqual(issue["unresolved_count"], 2)
        self.assertEqual(issue["priority_score"], 13)
        self.assertEqual(issue["escalation_rate"], 0.3333)
        self.assertEqual(issue["change_percent"], 200)
        self.assertTrue(issue["knowledge_gap"])
        self.assertEqual(set(issue["source_ticket_ids"]), set(current_ids))

    def test_knowledge_gap_changes_only_for_own_approved_runbooks(self):
        for _ in range(3):
            self.seed_ticket()
        self.seed_runbook()
        result = self.client.get("/api/v1/insights", headers=self.headers(role="viewer")).json
        issue = result["recurring_issues"][0]
        self.assertEqual(issue["draft_runbook_count"], 1)
        self.assertIn("Review", issue["recommended_action"])
        self.assertIsNone(issue["change_percent"])
        self.seed_runbook(status="approved")
        result = self.client.get("/api/v1/insights", headers=self.headers()).json
        self.assertFalse(result["recurring_issues"][0]["knowledge_gap"])

    def test_minimum_pattern_size_and_parameter_limits(self):
        self.seed_ticket()
        self.seed_ticket()
        self.assertEqual(self.client.get("/api/v1/insights", headers=self.headers()).json["recurring_issues"], [])
        for query in ("days=0", "days=91", "min_tickets=2", "limit=51", "tenant_id=company-b"):
            self.assertEqual(self.client.get(f"/api/v1/insights?{query}", headers=self.headers()).status_code, 400)

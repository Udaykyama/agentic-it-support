import sqlite3
import tempfile
from pathlib import Path

from sqlalchemy import func, select

from app.models import Runbook, Tenant, Ticket, new_id
from tests.helpers import AppTestCase, PAYLOAD


class CLITests(AppTestCase):
    def test_tenant_provisioning_disable_and_enable(self):
        runner = self.app.test_cli_runner()
        created = runner.invoke(args=[
            "tenants", "create", "--id", "company-c", "--name", "Company C",
            "--escalation-target", "helpdesk@c.example",
        ])
        self.assertEqual(created.exit_code, 0, created.output)
        with self.database.session() as db:
            self.assertTrue(db.get(Tenant, "company-c").active)
        disabled = runner.invoke(args=["tenants", "disable", "--id", "company-a"])
        self.assertEqual(disabled.exit_code, 0, disabled.output)
        self.assertEqual(self.client.get("/api/v1/me", headers=self.headers()).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/me", headers=self.headers("company-b")).status_code, 200)
        enabled = runner.invoke(args=["tenants", "enable", "--id", "company-a"])
        self.assertEqual(enabled.exit_code, 0, enabled.output)
        self.assertEqual(self.client.get("/api/v1/me", headers=self.headers()).status_code, 200)

    def source(self, directory, invalid=False):
        filename = Path(directory) / "legacy.db"
        with sqlite3.connect(filename) as db:
            db.execute("CREATE TABLE tickets (id TEXT, title TEXT, description TEXT, submitter TEXT, timestamp TEXT, status TEXT, category TEXT, resolution TEXT, confidence REAL)")
            db.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                new_id(), PAYLOAD["title"], PAYLOAD["description"], PAYLOAD["submitter"],
                "2025-01-01T12:00:00", "auto_resolved", "network", "Unverified old advice", 0.9,
            ))
            if invalid:
                db.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                    new_id(), " ", "Invalid old record", PAYLOAD["submitter"],
                    "2025-01-01T12:00:00", "auto_resolved", "network", None, 0.9,
                ))
        return filename

    def test_legacy_import_is_explicit_tenant_scoped_idempotent_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as folder:
            filename = self.source(folder)
            book_dir = Path(folder) / "runbooks"
            book_dir.mkdir()
            content = "# Network runbook\n\nUnverified legacy content for human review.\n"
            (book_dir / "network_runbook.md").write_text(content, encoding="utf-8")
            before = filename.read_bytes()
            arguments = ["import-legacy", "--tenant", "company-a", "--database", str(filename), "--runbooks", str(book_dir)]
            runner = self.app.test_cli_runner()
            result = runner.invoke(args=arguments)
            self.assertEqual(result.exit_code, 0, result.output)
            repeated = runner.invoke(args=arguments)
            self.assertEqual(repeated.exit_code, 0, repeated.output)
            self.assertEqual(filename.read_bytes(), before)
        with self.database.session("company-a") as db:
            tickets = db.scalars(select(Ticket)).all()
            self.assertEqual(len(tickets), 1)
            self.assertEqual(tickets[0].tenant_id, "company-a")
            self.assertEqual(tickets[0].status, "recommended")
            self.assertIsNone(tickets[0].resolution)
            self.assertEqual(tickets[0].recommendation, "Unverified old advice")
            books = db.scalars(select(Runbook)).all()
            self.assertEqual(len(books), 1)
            self.assertEqual(books[0].status, "draft")
            self.assertIsNone(books[0].embedding)
            self.assertEqual(books[0].steps, content)

    def test_legacy_validation_failure_rolls_back_all_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.app.test_cli_runner().invoke(args=[
                "import-legacy", "--tenant", "company-a", "--database", str(self.source(folder, invalid=True)),
            ])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("no destination changes", result.output)
        with self.database.session("company-a") as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Ticket)), 0)

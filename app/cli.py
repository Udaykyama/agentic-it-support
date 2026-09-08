import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click
from flask import current_app
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.auth import TENANT_ID_PATTERN
from app.db import audit
from app.ingest import CATEGORIES, RunbookDraft, TicketInput, request_fingerprint
from app.models import Runbook, Tenant, Ticket, new_id


def register_commands(app):
    @app.cli.group()
    def tenants():
        """Provision companies before assigning their trusted identity claims."""

    @tenants.command("create")
    @click.option("--id", "tenant_id", required=True)
    @click.option("--name", required=True)
    @click.option("--escalation-target", required=True)
    def create_tenant(tenant_id, name, escalation_target):
        if not TENANT_ID_PATTERN.fullmatch(tenant_id):
            raise click.ClickException("Company ID must contain 1-128 letters, digits, dots, underscores, or hyphens.")
        if not 1 <= len(name.strip()) <= 200 or not 1 <= len(escalation_target.strip()) <= 254:
            raise click.ClickException("A company name (max 200) and escalation target (max 254) are required.")
        db = current_app.extensions["database"]
        with db.session(tenant_id) as session:
            if session.get(Tenant, tenant_id):
                raise click.ClickException("That company already exists.")
            session.add(Tenant(id=tenant_id, name=name.strip(), escalation_target=escalation_target.strip()))
            session.flush()
            audit(session, tenant_id, "operator-cli", "tenant_created", None, new_id())
        click.echo(f"Created company {tenant_id}. Configure matching tenant and role claims in your identity provider.")

    def set_active(tenant_id, active):
        database = current_app.extensions["database"]
        with database.session(tenant_id) as session:
            tenant = session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
            if tenant is None:
                raise click.ClickException("Company not found.")
            tenant.active = active
            audit(session, tenant_id, "operator-cli", "tenant_enabled" if active else "tenant_disabled", None, new_id())
        click.echo(f"Company {tenant_id} {'enabled' if active else 'disabled'}.")

    @tenants.command("disable")
    @click.option("--id", "tenant_id", required=True)
    def disable_tenant(tenant_id):
        set_active(tenant_id, False)

    @tenants.command("enable")
    @click.option("--id", "tenant_id", required=True)
    def enable_tenant(tenant_id):
        set_active(tenant_id, True)

    @app.cli.command("import-legacy")
    @click.option("--tenant", "tenant_id", required=True)
    @click.option("--database", "filename", required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option("--runbooks", "runbook_directory", type=click.Path(exists=True, file_okay=False))
    def import_legacy(tenant_id, filename, runbook_directory):
        database = current_app.extensions["database"]
        database.tenant(tenant_id)
        source = sqlite3.connect(f"{Path(filename).resolve().as_uri()}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        imported, skipped, runbooks = 0, 0, 0
        try:
            with database.session(tenant_id) as session:
                for row in source.execute("SELECT * FROM tickets"):
                    raw = dict(row)
                    payload = TicketInput.model_validate({
                        "title": raw["title"], "description": raw["description"], "submitter": raw["submitter"],
                    })
                    ticket_id = str(uuid.UUID(raw["id"]))
                    fingerprint = request_fingerprint(payload)
                    existing = session.scalar(select(Ticket).where(Ticket.tenant_id == tenant_id, Ticket.id == ticket_id))
                    if existing:
                        if existing.request_hash != fingerprint:
                            raise click.ClickException("An existing ticket ID has different content; import stopped without changes.")
                        skipped += 1
                        continue
                    timestamp = datetime.fromisoformat(raw["timestamp"].replace("Z", "+00:00"))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    category = raw.get("category")
                    if category is not None and category not in CATEGORIES:
                        raise click.ClickException("A legacy category is invalid; import stopped without changes.")
                    confidence = raw.get("confidence")
                    if confidence is not None and not 0 <= confidence <= 1:
                        raise click.ClickException("A legacy confidence is invalid; import stopped without changes.")
                    recommendation = raw.get("resolution") or None
                    session.add(Ticket(
                        id=ticket_id, tenant_id=tenant_id, created_by="legacy-import",
                        **payload.model_dump(), created_at=timestamp, updated_at=timestamp,
                        status="recommended" if recommendation else "escalated",
                        category=category, confidence=confidence,
                        recommendation=recommendation, resolution=None,
                        reason="legacy_unverified", idempotency_key=f"legacy:{ticket_id}", request_hash=fingerprint,
                    ))
                    imported += 1
                if runbook_directory:
                    for path in sorted(Path(runbook_directory).glob("*.md")):
                        category = path.stem.removesuffix("_runbook")
                        if category not in CATEGORIES:
                            raise click.ClickException("A legacy runbook filename must use a supported category.")
                        content = path.read_text(encoding="utf-8")
                        if len(content) > 12000:
                            raise click.ClickException("A legacy runbook exceeds 12000 characters; split and review it before importing.")
                        fingerprint = hashlib.sha256(f"legacy:{content}".encode("utf-8")).hexdigest()
                        existing = session.scalar(select(Runbook.id).where(
                            Runbook.tenant_id == tenant_id, Runbook.source_fingerprint == fingerprint,
                        ))
                        if existing:
                            continue
                        title = content.splitlines()[0].lstrip("# ").strip() if content.strip() else ""
                        draft = RunbookDraft(
                            title=title, problem="Legacy content imported for review.",
                            root_cause="Unverified legacy hypothesis; inspect the original content below.",
                            steps=content, prevention="Review this legacy content before enabling recommendations.",
                        )
                        session.add(Runbook(
                            tenant_id=tenant_id, category=category, subcategory="legacy_import",
                            source_ticket_ids=[], source_fingerprint=fingerprint,
                            **draft.model_dump(exclude={"steps"}), steps=content,
                        ))
                        runbooks += 1
                audit(
                    session, tenant_id, "operator-cli", "legacy_imported", None, new_id(),
                    ticket_count=imported, skipped_count=skipped, runbook_count=runbooks,
                )
        except (sqlite3.Error, SQLAlchemyError, ValidationError, ValueError, KeyError, TypeError, OSError) as error:
            current_app.extensions["logger"].exception("legacy_import_failed")
            raise click.ClickException(
                f"Import failed ({type(error).__name__}); no destination changes were committed. Check the source schema and values."
            ) from None
        finally:
            source.close()
        click.echo(f"Imported {imported} tickets and {runbooks} draft runbooks; skipped {skipped} existing tickets.")
        click.echo("Legacy auto-resolutions are unverified recommendations, not confirmed fixes. The source files were not modified.")

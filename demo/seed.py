from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import timedelta
from urllib.request import Request, urlopen

from sqlalchemy import delete, func, select

from app.config import Settings
from app.db import Database
from app.models import AuditEvent, Job, Runbook, Tenant, Ticket, utcnow

DATABASE_NAME = "runbooksignal_demo"
MODEL_RESET_URL = "http://model-stub:8080/__demo/reset"
NAMESPACE = uuid.UUID("f50cb8e0-cbe6-4bcf-8c5d-b216a94679a6")

TENANTS = {
    "northstar-demo": {
        "name": "Northstar Research (Synthetic demo)",
        "escalation_target": "service-desk@northstar.example.com",
    },
    "harbor-demo": {
        "name": "Harbor Peak Logistics (Synthetic demo)",
        "escalation_target": "service-desk@harbor.example.com",
    },
}

SUBJECTS = {
    "northstar-requester": "10000000-0000-4000-8000-000000000001",
    "northstar-agent": "10000000-0000-4000-8000-000000000003",
    "northstar-admin": "10000000-0000-4000-8000-000000000004",
    "northstar-combined": "10000000-0000-4000-8000-000000000005",
    "harbor-admin": "20000000-0000-4000-8000-000000000001",
}


def stable_id(label):
    return str(uuid.uuid5(NAMESPACE, label))


def fingerprint(label):
    return hashlib.sha256(f"runbooksignal-synthetic-demo:{label}".encode("utf-8")).hexdigest()


def demo_vector(index):
    vector = [0.0] * 1536
    vector[index] = 1.0
    return vector


def ticket(label, tenant_id, actor, days_ago, **values):
    created_at = values.pop("now") - timedelta(days=days_ago)
    status = values.pop("status", "escalated")
    row = {
        "id": stable_id(f"ticket:{label}"),
        "tenant_id": tenant_id,
        "created_by": SUBJECTS[actor],
        "title": values.pop("title"),
        "description": values.pop("description"),
        "submitter": values.pop("submitter"),
        "category_hint": values.pop("category_hint", None),
        "created_at": created_at,
        "updated_at": created_at,
        "status": status,
        "category": values.pop("category"),
        "subcategory": values.pop("subcategory"),
        "confidence": values.pop("confidence", 0.96),
        "recommendation": values.pop("recommendation", None),
        "resolution": values.pop("resolution", None),
        "runbook_id": values.pop("runbook_id", None),
        "assigned_to": values.pop("assigned_to", None),
        "reason": values.pop("reason", None),
        "error_code": values.pop("error_code", None),
        "idempotency_key": f"demo-seed:{label}",
        "request_hash": fingerprint(f"ticket:{label}"),
        "resolved_at": values.pop("resolved_at", None),
    }
    if values:
        raise ValueError(f"Unexpected fixture fields for {label}: {sorted(values)}")
    return Ticket(**row)


def fixtures(now):
    northstar_target = TENANTS["northstar-demo"]["escalation_target"]
    northstar = [
        ticket(
            "northstar-vpn-requester", "northstar-demo", "northstar-requester", 1, now=now,
            title="VPN times out before connecting from home",
            description="Synthetic report: the approved VPN client reaches connecting, then times out on home Wi-Fi.",
            submitter="requester@northstar.example.com",
            category="network", subcategory="vpn_timeout",
            assigned_to=northstar_target, reason="no_relevant_runbook",
        ),
        ticket(
            "northstar-vpn-resolved", "northstar-demo", "northstar-agent", 3, now=now,
            title="VPN timeout on a managed laptop",
            description="Synthetic report: VPN timed out on Ethernet and Wi-Fi before an operator investigated.",
            submitter="analyst@northstar.example.com",
            status="resolved", category="network", subcategory="vpn_timeout",
            resolution="Synthetic operator verified the service recovered, refreshed the approved client session, and confirmed access to a permitted test resource.",
            resolved_at=now - timedelta(days=3) + timedelta(hours=2),
            assigned_to=northstar_target, reason="no_relevant_runbook",
        ),
        ticket(
            "northstar-vpn-agent", "northstar-demo", "northstar-agent", 6, now=now,
            title="Repeated VPN timeout after sign-in",
            description="Synthetic report: the managed VPN client times out after identity verification.",
            submitter="engineer@northstar.example.com",
            category="network", subcategory="vpn_timeout",
            assigned_to=northstar_target, reason="no_relevant_runbook",
        ),
        ticket(
            "northstar-vpn-combined", "northstar-demo", "northstar-combined", 10, now=now,
            title="VPN connection timeout on a second network",
            description="Synthetic report: the same VPN timeout occurred on a second trusted network.",
            submitter="combined@northstar.example.com",
            category="network", subcategory="vpn_timeout",
            assigned_to=northstar_target, reason="no_relevant_runbook",
        ),
        ticket(
            "northstar-vpn-previous", "northstar-demo", "northstar-admin", 20, now=now,
            title="Earlier VPN timeout baseline",
            description="Synthetic preceding-window report used only to compare recurrence volume.",
            submitter="operations@northstar.example.com",
            status="resolved", category="network", subcategory="vpn_timeout",
            resolution="Synthetic operator confirmed the earlier interruption ended and validated a permitted test resource.",
            resolved_at=now - timedelta(days=20) + timedelta(hours=1),
            assigned_to=northstar_target, reason="no_relevant_runbook",
        ),
        ticket(
            "northstar-role-boundary-failure", "northstar-demo", "northstar-agent", 4, now=now,
            title="Synthetic failed classification for role-boundary review",
            description="Synthetic saved failure used to show that requester-plus-viewer visibility does not grant another user's retry permission.",
            submitter="role-check@northstar.example.com",
            status="failed", category="other", subcategory="needs_human_review",
            confidence=0.41, error_code="invalid_model_response",
        ),
    ]

    harbor_book_id = stable_id("runbook:harbor-scanner")
    harbor_steps = (
        "1. Verify the requester and scanner asset tag.\n"
        "2. Confirm the approved warehouse service status.\n"
        "3. Reseat the scanner in its approved cradle and retry synchronization once.\n"
        "4. Verify one synthetic test scan; escalate when synchronization remains unavailable."
    )
    harbor_book = Runbook(
        id=harbor_book_id,
        tenant_id="harbor-demo",
        category="hardware",
        subcategory="warehouse_scanner_offline",
        title="Review an offline warehouse scanner",
        problem="Synthetic handheld scanners can stop synchronizing after leaving an approved cradle.",
        root_cause="Hypothesis: power, cradle connectivity, or service availability may interrupt synchronization.",
        steps=harbor_steps,
        prevention="Review confirmed outcomes before changing charging, network, or device guidance.",
        status="approved",
        source_ticket_ids=[
            stable_id("ticket:harbor-scanner-recommended"),
            stable_id("ticket:harbor-scanner-resolved"),
            stable_id("ticket:harbor-scanner-second"),
        ],
        source_fingerprint=fingerprint("runbook:harbor-scanner"),
        embedding=demo_vector(1),
        created_at=now - timedelta(days=5),
        approved_by=SUBJECTS["harbor-admin"],
        approved_at=now - timedelta(days=4),
    )
    harbor = [
        ticket(
            "harbor-scanner-recommended", "harbor-demo", "harbor-admin", 1, now=now,
            title="Warehouse scanner is offline after leaving its cradle",
            description="Synthetic report: a handheld scanner stopped synchronizing after a charging cycle.",
            submitter="picker-one@harbor.example.com",
            status="recommended", category="hardware", subcategory="warehouse_scanner_offline",
            recommendation=harbor_steps, runbook_id=harbor_book_id, reason="approved_runbook_match",
        ),
        ticket(
            "harbor-scanner-resolved", "harbor-demo", "harbor-admin", 4, now=now,
            title="Handheld scanner would not synchronize",
            description="Synthetic report: a managed scanner remained offline in the packing area.",
            submitter="packer@harbor.example.com",
            status="resolved", category="hardware", subcategory="warehouse_scanner_offline",
            recommendation=harbor_steps, runbook_id=harbor_book_id, reason="approved_runbook_match",
            resolution="Synthetic operator reseated the scanner in its approved cradle and verified one test scan synchronized.",
            resolved_at=now - timedelta(days=4) + timedelta(hours=1),
        ),
        ticket(
            "harbor-scanner-second", "harbor-demo", "harbor-admin", 8, now=now,
            title="Second warehouse scanner offline",
            description="Synthetic report: another handheld scanner could not synchronize in the same window.",
            submitter="picker-two@harbor.example.com",
            status="recommended", category="hardware", subcategory="warehouse_scanner_offline",
            recommendation=harbor_steps, runbook_id=harbor_book_id, reason="approved_runbook_match",
        ),
        ticket(
            "harbor-scanner-previous", "harbor-demo", "harbor-admin", 19, now=now,
            title="Earlier scanner synchronization baseline",
            description="Synthetic preceding-window scanner report used for recurrence comparison.",
            submitter="supervisor@harbor.example.com",
            status="resolved", category="hardware", subcategory="warehouse_scanner_offline",
            recommendation=harbor_steps, runbook_id=harbor_book_id, reason="approved_runbook_match",
            resolution="Synthetic operator verified the earlier scanner synchronized after approved cradle checks.",
            resolved_at=now - timedelta(days=19) + timedelta(hours=1),
        ),
    ]
    return {
        "northstar-demo": {"tickets": northstar, "runbooks": []},
        "harbor-demo": {"tickets": harbor, "runbooks": [harbor_book]},
    }


def validate_demo_database(database, allow_inactive=False):
    url = database.engine.url
    if (
        url.database != DATABASE_NAME or url.host != "db" or url.port != 5432
        or url.username != "neuraldesk_app"
    ):
        raise RuntimeError("Refusing to seed: the runtime database is not the fixed internal demo database.")
    with database.session() as session:
        existing = {tenant.id: tenant for tenant in session.scalars(select(Tenant)).all()}
    unknown = sorted(set(existing) - set(TENANTS))
    if unknown:
        raise RuntimeError("Refusing to seed: the demo database contains an unknown company.")
    for tenant_id, expected in TENANTS.items():
        tenant = existing.get(tenant_id)
        if tenant is not None and (
            tenant.name != expected["name"]
            or tenant.escalation_target != expected["escalation_target"]
            or (not allow_inactive and not tenant.active)
        ):
            raise RuntimeError(f"Refusing to seed: {tenant_id} is not an owned synthetic demo company.")


def reset_model_stub():
    token = os.environ.get("DEMO_STUB_CONTROL_TOKEN", "")
    if not token:
        raise RuntimeError("DEMO_STUB_CONTROL_TOKEN is required for an explicit reset.")
    request = Request(
        MODEL_RESET_URL,
        data=b"",
        method="POST",
        headers={"X-Demo-Control-Token": token},
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError) as error:
        raise RuntimeError("The internal deterministic model stub could not be reset.") from error
    if payload != {"status": "reset"}:
        raise RuntimeError("The internal deterministic model stub returned an unexpected reset response.")


def ensure_tenants(database, restore=False):
    with database.session() as session:
        for tenant_id, values in TENANTS.items():
            tenant = session.get(Tenant, tenant_id)
            if tenant is None:
                session.add(Tenant(id=tenant_id, active=True, **values))
            elif restore:
                tenant.active = True


def reset_records(database):
    with database.session() as session:
        for tenant_id in TENANTS:
            database.bind_tenant(session, tenant_id)
            session.execute(delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id))
            session.execute(delete(Ticket).where(Ticket.tenant_id == tenant_id))
            session.execute(delete(Runbook).where(Runbook.tenant_id == tenant_id))
        session.execute(delete(Job).where(Job.tenant_id.in_(TENANTS)))


def ensure_records(database, records):
    for tenant_id, values in records.items():
        with database.session(tenant_id) as session:
            for runbook in values["runbooks"]:
                existing = session.get(Runbook, runbook.id)
                if existing is None:
                    session.add(runbook)
                elif existing.source_fingerprint != runbook.source_fingerprint:
                    raise RuntimeError("A stable synthetic runbook ID contains unexpected data.")
            for seeded_ticket in values["tickets"]:
                existing = session.get(Ticket, seeded_ticket.id)
                if existing is None:
                    session.add(seeded_ticket)
                elif existing.request_hash != seeded_ticket.request_hash:
                    raise RuntimeError("A stable synthetic ticket ID contains unexpected data.")


def print_summary(database, action):
    values = []
    for tenant_id in TENANTS:
        with database.session(tenant_id) as session:
            values.append((
                tenant_id,
                session.scalar(select(func.count()).select_from(Ticket)),
                session.scalar(select(func.count()).select_from(Runbook)),
            ))
    details = ", ".join(f"{tenant}: {tickets} tickets/{runbooks} runbooks" for tenant, tickets, runbooks in values)
    print(f"Synthetic demo {action}: {details}.")


def main():
    parser = argparse.ArgumentParser(description="Manage only the fixed RunbookSignal synthetic demo fixtures.")
    parser.add_argument("action", choices=("ensure", "reset"))
    args = parser.parse_args()
    settings = Settings.from_env()
    if not settings.demo_mode:
        parser.error("This utility runs only with validated DEMO_MODE=true configuration.")
    database = Database(settings)
    try:
        database.validate_runtime_role()
        validate_demo_database(database, allow_inactive=args.action == "reset")
        if args.action == "reset":
            reset_model_stub()
            reset_records(database)
        ensure_tenants(database, restore=args.action == "reset")
        ensure_records(database, fixtures(utcnow()))
        print_summary(database, "reset" if args.action == "reset" else "ready")
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()

import re

from flask import Blueprint, current_app, g, jsonify, request, session as browser_session
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.auth import OPERATE, READ_COMPANY, WRITE_TICKETS, require_roles
from app.db import audit
from app.errors import APIError, validate_input
from app.ingest import GenerateInput, ResolutionInput, TicketInput, request_fingerprint
from app.insights import recurring_incidents
from app.jobs import enqueue
from app.models import AuditEvent, Job, Runbook, Ticket, new_id, utcnow
from app.rate_limit import enforce_limit
from app.resolver import confirm_resolution
from app.runbook_gen import source_tickets

api = Blueprint("api", __name__, url_prefix="/api/v1")
TICKET_STATUSES = {"pending", "processing", "recommended", "escalated", "resolved", "failed"}


def database():
    return current_app.extensions["database"]


def integer_arg(name, default, minimum, maximum):
    values = request.args.getlist(name)
    if len(values) > 1:
        raise APIError(400, "invalid_query", f"Supply {name} only once.")
    value = request.args.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", value) or len(value) > 9:
        raise APIError(400, "invalid_query", f"{name} must be an integer.")
    number = int(value)
    if not minimum <= number <= maximum:
        raise APIError(400, "invalid_query", f"{name} must be between {minimum} and {maximum}.")
    return number


def pagination_args(extra=()):
    unknown = set(request.args) - {"limit", "offset", *extra}
    if unknown:
        raise APIError(400, "invalid_query", "The query contains unsupported parameters.")
    return integer_arg("limit", 25, 1, 100), integer_arg("offset", 0, 0, 10000)


def paginated(session, model, predicates, limit, offset):
    total = session.scalar(select(func.count()).select_from(model).where(*predicates))
    records = session.scalars(
        select(model).where(*predicates).order_by(model.created_at.desc(), model.id.desc())
        .limit(limit).offset(offset)
    ).all()
    return records, {"limit": limit, "offset": offset, "total": total, "has_more": offset + len(records) < total}


def ticket_predicates(for_write=False):
    predicates = [Ticket.tenant_id == g.principal.tenant_id]
    if not g.principal.has_role(OPERATE if for_write else READ_COMPANY):
        predicates.append(Ticket.created_by == g.principal.subject)
    return predicates


def serialize_ticket(ticket):
    can_retry = (
        ticket.status == "failed" and g.principal.has_role(WRITE_TICKETS)
        and (g.principal.has_role(OPERATE) or ticket.created_by == g.principal.subject)
    )
    return {**ticket.to_dict(), "can_retry": can_retry}


def find_ticket(session, ticket_id, lock=False, for_write=False):
    query = select(Ticket).where(*ticket_predicates(for_write), Ticket.id == str(ticket_id))
    ticket = session.scalar(query.with_for_update() if lock else query)
    if ticket is None:
        raise APIError(404, "not_found", "The ticket was not found.")
    return ticket


def find_runbook(session, runbook_id, lock=False):
    query = select(Runbook).where(
        Runbook.tenant_id == g.principal.tenant_id, Runbook.id == str(runbook_id),
    )
    runbook = session.scalar(query.with_for_update() if lock else query)
    if runbook is None:
        raise APIError(404, "not_found", "The runbook was not found.")
    return runbook


def require_ai():
    settings = current_app.config["SETTINGS"]
    if not settings.ai_enabled:
        raise APIError(503, "ai_disabled", "AI processing is disabled. An administrator must enable an approved model provider.")
    enforce_limit(current_app.extensions["redis"], "tenant_ai", g.principal.tenant_id, settings.ai_rate_limit_per_minute)


@api.get("/me")
@require_roles()
def me():
    return {
        "user": g.principal.to_dict(),
        "tenant": {"id": g.tenant.id, "name": g.tenant.name},
        "csrf_token": browser_session.get("csrf_token") if g.authentication_method == "session" else None,
        "capabilities": {"ai_enabled": current_app.config["SETTINGS"].ai_enabled},
    }


def duplicate_ticket(session, key, fingerprint):
    ticket = session.scalar(select(Ticket).where(
        Ticket.tenant_id == g.principal.tenant_id, Ticket.created_by == g.principal.subject,
        Ticket.idempotency_key == key,
    ))
    if ticket is None:
        return None
    if ticket.request_hash != fingerprint:
        raise APIError(409, "idempotency_conflict", "This Idempotency-Key was already used with a different request.")
    return {"ticket": serialize_ticket(ticket), "job": None, "duplicate": True}


@api.post("/tickets")
@require_roles(*WRITE_TICKETS)
def ingest_ticket():
    payload = validate_input(TicketInput, request.get_json())
    if not g.principal.has_role(OPERATE):
        if not g.principal.email or payload.submitter.lower() != g.principal.email.lower():
            raise APIError(403, "submitter_mismatch", "Use the email address supplied by your sign-in provider.")
    key = request.headers.get("Idempotency-Key")
    if key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise APIError(400, "invalid_idempotency_key", "Idempotency-Key must contain 1-128 letters, digits, dots, underscores, colons, or hyphens.")
    fingerprint = request_fingerprint(payload)
    settings = current_app.config["SETTINGS"]
    try:
        with database().session(g.principal.tenant_id) as db:
            if key:
                existing = duplicate_ticket(db, key, fingerprint)
                if existing:
                    return existing, 200
            if settings.ai_enabled:
                require_ai()
            ticket = Ticket(
                id=new_id(), tenant_id=g.principal.tenant_id, created_by=g.principal.subject,
                idempotency_key=key, request_hash=fingerprint, **payload.model_dump(),
                status="pending" if settings.ai_enabled else "escalated",
                assigned_to=None if settings.ai_enabled else g.tenant.escalation_target,
                reason=None if settings.ai_enabled else "ai_disabled",
            )
            db.add(ticket)
            job = None
            if settings.ai_enabled:
                job = enqueue(db, g.principal.tenant_id, "classify_ticket", g.principal.subject, g.request_id, ticket.id)
            audit(db, g.principal.tenant_id, g.principal.subject, "ticket_created", ticket.id, g.request_id)
            db.flush()
            body = {"ticket": serialize_ticket(ticket), "job": job.to_dict() if job else None, "duplicate": False}
    except IntegrityError:
        if key:
            with database().session(g.principal.tenant_id) as db:
                existing = duplicate_ticket(db, key, fingerprint)
                if existing:
                    return existing, 200
        raise
    response = jsonify(body)
    response.headers["Location"] = f"/api/v1/tickets/{ticket.id}"
    return response, 202 if settings.ai_enabled else 201


@api.get("/tickets")
@require_roles()
def list_tickets():
    limit, offset = pagination_args(extra=("status",))
    predicates = ticket_predicates()
    if "status" in request.args:
        if len(request.args.getlist("status")) != 1 or request.args["status"] not in TICKET_STATUSES:
            raise APIError(400, "invalid_query", "status must be a supported ticket status.")
        predicates.append(Ticket.status == request.args["status"])
    with database().session(g.principal.tenant_id) as db:
        records, pagination = paginated(db, Ticket, predicates, limit, offset)
        return {"tickets": [serialize_ticket(ticket) for ticket in records], "pagination": pagination}


@api.get("/tickets/<uuid:ticket_id>")
@require_roles()
def get_ticket(ticket_id):
    with database().session(g.principal.tenant_id) as db:
        return {"ticket": serialize_ticket(find_ticket(db, ticket_id))}


@api.post("/tickets/<uuid:ticket_id>/resolve")
@require_roles(*OPERATE)
def resolve_ticket(ticket_id):
    payload = validate_input(ResolutionInput, request.get_json())
    with database().session(g.principal.tenant_id) as db:
        ticket = find_ticket(db, ticket_id, lock=True, for_write=True)
        confirm_resolution(db, ticket, payload.resolution, g.principal.subject, g.request_id)
        db.flush()
        return {"ticket": serialize_ticket(ticket)}


@api.post("/tickets/<uuid:ticket_id>/retry")
@require_roles(*WRITE_TICKETS)
def retry_ticket(ticket_id):
    require_ai()
    with database().session(g.principal.tenant_id) as db:
        ticket = find_ticket(db, ticket_id, lock=True, for_write=True)
        if ticket.status != "failed":
            raise APIError(409, "invalid_state", "Only failed tickets can be retried.")
        ticket.status, ticket.error_code = "pending", None
        job = enqueue(db, g.principal.tenant_id, "classify_ticket", g.principal.subject, g.request_id, ticket.id)
        audit(db, g.principal.tenant_id, g.principal.subject, "ticket_retried", ticket.id, g.request_id)
        db.flush()
        return {"ticket": serialize_ticket(ticket), "job": job.to_dict()}, 202


@api.get("/runbooks")
@require_roles()
def list_runbooks():
    limit, offset = pagination_args()
    predicates = [Runbook.tenant_id == g.principal.tenant_id]
    if not g.principal.has_role(READ_COMPANY):
        predicates.append(Runbook.status == "approved")
    with database().session(g.principal.tenant_id) as db:
        records, pagination = paginated(db, Runbook, predicates, limit, offset)
        values = []
        for runbook in records:
            value = runbook.to_dict()
            if not g.principal.has_role(READ_COMPANY):
                value["source_ticket_ids"] = []
                value["approved_by"] = None
            values.append(value)
        return {"runbooks": values, "pagination": pagination}


@api.post("/runbooks/generate")
@require_roles(*OPERATE)
def generate_runbook():
    payload = validate_input(GenerateInput, request.get_json())
    require_ai()
    try:
        with database().session(g.principal.tenant_id) as db:
            ids, fingerprint = source_tickets(db, g.principal.tenant_id, payload.category, payload.subcategory)
            key = f"generate:{fingerprint}"
            existing = db.scalar(select(Job).where(Job.tenant_id == g.principal.tenant_id, Job.deduplication_key == key))
            if existing:
                return {"job": existing.to_dict()}, 200
            job = enqueue(
                db, g.principal.tenant_id, "generate_runbook", g.principal.subject, g.request_id,
                payload={**payload.model_dump(), "source_ticket_ids": ids, "source_fingerprint": fingerprint},
                deduplication_key=key,
            )
            audit(db, g.principal.tenant_id, g.principal.subject, "runbook_generation_requested", job.id, g.request_id)
            db.flush()
            result = job.to_dict()
    except IntegrityError:
        with database().session(g.principal.tenant_id) as db:
            existing = db.scalar(select(Job).where(Job.tenant_id == g.principal.tenant_id, Job.deduplication_key == key))
            if existing:
                return {"job": existing.to_dict()}, 200
        raise
    return {"job": result}, 202


@api.post("/runbooks/<uuid:runbook_id>/approve")
@require_roles("admin")
def approve_runbook(runbook_id):
    require_ai()
    with database().session(g.principal.tenant_id) as db:
        runbook = find_runbook(db, runbook_id, lock=True)
        if runbook.status != "draft":
            raise APIError(409, "invalid_state", "Only draft runbooks can be approved.")
        job = enqueue(db, g.principal.tenant_id, "approve_runbook", g.principal.subject, g.request_id, runbook.id)
        runbook.status, runbook.approval_job_id, runbook.error_code = "approving", job.id, None
        audit(db, g.principal.tenant_id, g.principal.subject, "runbook_approval_requested", runbook.id, g.request_id)
        db.flush()
        return {"runbook": runbook.to_dict(), "job": job.to_dict()}, 202


@api.post("/runbooks/<uuid:runbook_id>/revoke")
@require_roles("admin")
def revoke_runbook(runbook_id):
    with database().session(g.principal.tenant_id) as db:
        runbook = find_runbook(db, runbook_id, lock=True)
        if runbook.status != "rejected":
            runbook.status, runbook.embedding, runbook.error_code = "rejected", None, None
            audit(db, g.principal.tenant_id, g.principal.subject, "runbook_revoked", runbook.id, g.request_id)
        db.flush()
        return {"runbook": runbook.to_dict()}


def find_job(db, job_id, lock=False):
    predicates = [Job.tenant_id == g.principal.tenant_id, Job.id == str(job_id)]
    if not g.principal.has_role(READ_COMPANY):
        predicates.append(Job.actor == g.principal.subject)
    query = select(Job).where(*predicates)
    job = db.scalar(query.with_for_update() if lock else query)
    if job is None:
        raise APIError(404, "not_found", "The job was not found.")
    return job


@api.get("/jobs/<uuid:job_id>")
@require_roles()
def get_job(job_id):
    with database().session(g.principal.tenant_id) as db:
        return {"job": find_job(db, job_id).to_dict()}


@api.post("/jobs/<uuid:job_id>/retry")
@require_roles(*OPERATE)
def retry_job(job_id):
    require_ai()
    with database().session(g.principal.tenant_id) as db:
        job = find_job(db, job_id, lock=True)
        if job.status != "failed":
            raise APIError(409, "invalid_state", "Only failed jobs can be retried.")
        if job.kind == "classify_ticket":
            raise APIError(409, "use_ticket_retry", "Retry classification from the ticket endpoint.")
        if job.kind == "approve_runbook":
            if not g.principal.has_role({"admin"}):
                raise APIError(403, "forbidden", "Only an administrator can retry runbook approval.")
            runbook = find_runbook(db, job.resource_id, lock=True)
            if runbook.status != "draft":
                raise APIError(409, "invalid_state", "Only draft runbooks can be approved.")
            runbook.status, runbook.approval_job_id, runbook.error_code = "approving", job.id, None
        job.status, job.attempts, job.available_at = "queued", 0, utcnow()
        job.lease_token, job.lease_expires_at, job.last_error_code = None, None, None
        job.actor, job.request_id = g.principal.subject, g.request_id
        audit(db, g.principal.tenant_id, g.principal.subject, "job_retried", job.id, g.request_id)
        db.flush()
        return {"job": job.to_dict()}, 202


@api.get("/stats")
@require_roles()
def stats():
    with database().session(g.principal.tenant_id) as db:
        statuses = {status: 0 for status in sorted(TICKET_STATUSES)}
        statuses.update(dict(db.execute(
            select(Ticket.status, func.count()).where(*ticket_predicates()).group_by(Ticket.status)
        ).all()))
        categories = dict(db.execute(
            select(Ticket.category, func.count()).where(*ticket_predicates(), Ticket.category.is_not(None))
            .group_by(Ticket.category)
        ).all())
        runbook_filters = [Runbook.tenant_id == g.principal.tenant_id]
        if not g.principal.has_role(READ_COMPANY):
            runbook_filters.append(Runbook.status == "approved")
        runbooks = {status: 0 for status in ("draft", "approving", "approved", "rejected")}
        runbooks.update(dict(db.execute(
            select(Runbook.status, func.count()).where(*runbook_filters).group_by(Runbook.status)
        ).all()))
        total = sum(statuses.values())
        return {
            "total": total, "statuses": statuses, "categories": categories, "runbooks": runbooks,
            "resolution_rate": statuses["resolved"] / total if total else 0,
        }


@api.get("/insights")
@require_roles(*READ_COMPANY)
def insights():
    if set(request.args) - {"days", "min_tickets", "limit"}:
        raise APIError(400, "invalid_query", "The query contains unsupported parameters.")
    days = integer_arg("days", 14, 1, 90)
    minimum = integer_arg("min_tickets", 3, 3, 1000)
    limit = integer_arg("limit", 20, 1, 50)
    with database().session(g.principal.tenant_id) as db:
        return recurring_incidents(db, g.principal.tenant_id, days, minimum, limit)


@api.get("/audit")
@require_roles("admin")
def audit_events():
    limit, offset = pagination_args()
    with database().session(g.principal.tenant_id) as db:
        records, pagination = paginated(db, AuditEvent, [AuditEvent.tenant_id == g.principal.tenant_id], limit, offset)
        return {"events": [event.to_dict() for event in records], "pagination": pagination}

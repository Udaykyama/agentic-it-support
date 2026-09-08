from datetime import timedelta

from openai import APIConnectionError, APITimeoutError, InternalServerError, OpenAIError, RateLimitError
from sqlalchemy import and_, or_, select

from app.db import audit
from app.errors import JobLeaseLost, ModelResponseError
from app.models import Job, Runbook, Tenant, Ticket, new_id, utcnow
from app.router import route_ticket
from app.runbook_kb import has_approved_runbook, search_runbook


def enqueue(session, tenant_id, kind, actor, request_id, resource_id=None, payload=None, deduplication_key=None):
    job = Job(
        id=new_id(), tenant_id=tenant_id, kind=kind, actor=actor, request_id=request_id,
        resource_id=resource_id, payload=payload or {}, deduplication_key=deduplication_key,
    )
    session.add(job)
    return job


def _terminal_failure(session, job, code):
    job.status, job.last_error_code = "failed", code
    job.lease_token, job.lease_expires_at = None, None
    if job.kind == "classify_ticket":
        ticket = session.scalar(select(Ticket).where(
            Ticket.tenant_id == job.tenant_id, Ticket.id == job.resource_id,
        ).with_for_update())
        if ticket and ticket.status in {"pending", "processing"}:
            ticket.status, ticket.error_code = "failed", code
    elif job.kind == "approve_runbook":
        runbook = session.scalar(select(Runbook).where(
            Runbook.tenant_id == job.tenant_id, Runbook.id == job.resource_id,
        ).with_for_update())
        if runbook and runbook.status == "approving" and runbook.approval_job_id == job.id:
            runbook.status, runbook.error_code = "draft", code
            runbook.approved_by = None
    audit(session, job.tenant_id, "worker", "job_failed", job.id, job.request_id, error_code=code)


def claim_next(database, settings):
    now = utcnow()
    with database.session() as session:
        job = session.scalar(
            select(Job).join(Tenant, Tenant.id == Job.tenant_id).where(
                Tenant.active.is_(True),
                or_(
                    and_(Job.status.in_(("queued", "retrying")), Job.available_at <= now),
                    and_(Job.status == "running", Job.lease_expires_at <= now),
                ),
            ).order_by(Job.available_at, Job.created_at, Job.id)
            .limit(1).with_for_update(skip_locked=True, of=Job)
        )
        if job is None:
            return None
        if job.attempts >= settings.job_max_attempts:
            database.bind_tenant(session, job.tenant_id)
            _terminal_failure(session, job, "worker_lease_expired")
            return None
        job.status = "running"
        job.attempts += 1
        job.lease_token = new_id()
        job.lease_expires_at = now + timedelta(seconds=settings.job_lease_seconds)
        session.flush()
        return job


def locked_job(session, claimed):
    job = session.scalar(select(Job).where(
        Job.id == claimed.id, Job.tenant_id == claimed.tenant_id, Job.status == "running",
        Job.lease_token == claimed.lease_token, Job.lease_expires_at > utcnow(),
    ).with_for_update())
    if job is None:
        raise JobLeaseLost("The job lease expired or was replaced")
    return job


def complete_job(session, job):
    job.status = "completed"
    job.lease_token, job.lease_expires_at, job.last_error_code = None, None, None
    audit(session, job.tenant_id, "worker", "job_completed", job.id, job.request_id, kind=job.kind)


def fail_job(database, settings, claimed, code, retryable):
    with database.session(claimed.tenant_id) as session:
        job = locked_job(session, claimed)
        if retryable and job.attempts < settings.job_max_attempts:
            job.status, job.last_error_code = "retrying", code
            job.available_at = utcnow() + timedelta(seconds=min(300, 10 * 2 ** (job.attempts - 1)))
            job.lease_token, job.lease_expires_at = None, None
            audit(session, job.tenant_id, "worker", "job_retry_scheduled", job.id, job.request_id, error_code=code)
        else:
            _terminal_failure(session, job, code)


class JobProcessor:
    def __init__(self, database, settings, ai, logger):
        self.database, self.settings, self.ai, self.logger = database, settings, ai, logger

    def process(self, claimed):
        context = {
            "job_id": claimed.id, "tenant_id": claimed.tenant_id,
            "request_id": claimed.request_id, "attempt": claimed.attempts,
        }
        self.logger.info("job_started", extra=context)
        try:
            if not self.settings.ai_enabled:
                raise ModelResponseError("ai_disabled")
            handlers = {
                "classify_ticket": self.classify_ticket,
                "generate_runbook": self.generate_runbook,
                "approve_runbook": self.approve_runbook,
            }
            handler = handlers.get(claimed.kind)
            if handler is None:
                raise ModelResponseError("unknown_job_kind")
            handler(claimed)
        except (OpenAIError, ModelResponseError) as error:
            retryable = isinstance(error, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError))
            code = "model_unavailable" if retryable else "invalid_model_response"
            if isinstance(error, ModelResponseError):
                code = str(error)
            self.logger.warning("job_processing_failed", extra={
                **context, "error_code": code, "error_type": type(error).__name__,
            })
            try:
                fail_job(self.database, self.settings, claimed, code, retryable)
            except JobLeaseLost:
                self.logger.warning("job_lease_lost", extra=context)
        except JobLeaseLost:
            self.logger.warning("job_lease_lost", extra=context)
        else:
            self.logger.info("job_completed", extra=context)

    def classify_ticket(self, claimed):
        with self.database.session(claimed.tenant_id) as session:
            job = locked_job(session, claimed)
            ticket = session.scalar(select(Ticket).where(
                Ticket.tenant_id == claimed.tenant_id, Ticket.id == claimed.resource_id,
            ).with_for_update())
            if ticket is None:
                raise ModelResponseError("source_unavailable")
            if ticket.status == "resolved":
                complete_job(session, job)
                return
            ticket.status = "processing"
        classification = self.ai.classify(ticket)
        embedding = None
        if classification.confidence >= self.settings.confidence_threshold:
            with self.database.session(claimed.tenant_id) as session:
                has_runbook = has_approved_runbook(session, claimed.tenant_id, classification.category)
            if has_runbook:
                embedding = self.ai.embed(f"{classification.subcategory}\n{ticket.title}\n{ticket.description}")
        with self.database.session(claimed.tenant_id) as session:
            job = locked_job(session, claimed)
            ticket = session.scalar(select(Ticket).where(
                Ticket.tenant_id == claimed.tenant_id, Ticket.id == claimed.resource_id,
            ).with_for_update())
            tenant = session.get(Tenant, claimed.tenant_id)
            if ticket is None or tenant is None or not tenant.active:
                raise ModelResponseError("source_unavailable")
            if ticket.status != "resolved":
                runbook = None
                if embedding is not None:
                    runbook = search_runbook(
                        session, claimed.tenant_id, classification.category,
                        embedding, self.settings.similarity_threshold,
                    )
                decision = route_ticket(
                    classification, runbook, self.settings.confidence_threshold, tenant.escalation_target,
                )
                for name, value in decision.items():
                    setattr(ticket, name, value)
                audit(session, claimed.tenant_id, "worker", "ticket_triaged", ticket.id, job.request_id,
                      status=ticket.status, category=ticket.category, reason=ticket.reason)
            complete_job(session, job)

    def generate_runbook(self, claimed):
        payload = claimed.payload
        with self.database.session(claimed.tenant_id) as session:
            locked_job(session, claimed)
            tickets = session.scalars(select(Ticket).where(
                Ticket.tenant_id == claimed.tenant_id, Ticket.id.in_(payload["source_ticket_ids"]),
                Ticket.category == payload["category"], Ticket.subcategory == payload["subcategory"],
            ).order_by(Ticket.created_at.desc(), Ticket.id.desc())).all()
        if len(tickets) < 3:
            raise ModelResponseError("source_unavailable")
        draft = self.ai.draft_runbook(payload["category"], payload["subcategory"], tickets)
        with self.database.session(claimed.tenant_id) as session:
            job = locked_job(session, claimed)
            existing = session.scalar(select(Runbook).where(
                Runbook.tenant_id == claimed.tenant_id,
                Runbook.source_fingerprint == payload["source_fingerprint"],
            ))
            if existing is None:
                existing = Runbook(
                    id=new_id(), tenant_id=claimed.tenant_id,
                    category=payload["category"], subcategory=payload["subcategory"],
                    source_ticket_ids=payload["source_ticket_ids"],
                    source_fingerprint=payload["source_fingerprint"], **draft.model_dump(),
                )
                session.add(existing)
                audit(session, claimed.tenant_id, job.actor, "runbook_drafted", existing.id, job.request_id)
            job.resource_id = existing.id
            complete_job(session, job)

    def approve_runbook(self, claimed):
        with self.database.session(claimed.tenant_id) as session:
            job = locked_job(session, claimed)
            runbook = session.scalar(select(Runbook).where(
                Runbook.tenant_id == claimed.tenant_id, Runbook.id == claimed.resource_id,
            ))
            if runbook is None:
                raise ModelResponseError("source_unavailable")
            if runbook.status != "approving" or runbook.approval_job_id != job.id:
                complete_job(session, job)
                return
        embedding = self.ai.embed(f"{runbook.title}\n{runbook.problem}\n{runbook.steps}")
        with self.database.session(claimed.tenant_id) as session:
            job = locked_job(session, claimed)
            runbook = session.scalar(select(Runbook).where(
                Runbook.tenant_id == claimed.tenant_id, Runbook.id == claimed.resource_id,
            ).with_for_update())
            if runbook and runbook.status == "approving" and runbook.approval_job_id == job.id:
                runbook.embedding, runbook.status = embedding, "approved"
                runbook.approved_at, runbook.approved_by = utcnow(), job.actor
                runbook.error_code = None
                audit(session, claimed.tenant_id, job.actor, "runbook_approved", runbook.id, job.request_id)
            complete_job(session, job)

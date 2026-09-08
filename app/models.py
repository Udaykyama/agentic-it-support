from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey,
    ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid.uuid4())


def isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    escalation_target: Mapped[str] = mapped_column(String(254))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Runbook(Base):
    __tablename__ = "runbooks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_runbooks_tenant_id"),
        UniqueConstraint("tenant_id", "source_fingerprint", name="uq_runbooks_source"),
        CheckConstraint("status IN ('draft', 'approving', 'approved', 'rejected')", name="ck_runbook_status"),
        Index("ix_runbooks_tenant_pattern", "tenant_id", "category", "subcategory", "status"),
        Index(
            "ix_runbooks_embedding", "embedding", postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("status = 'approved'"),
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(16))
    subcategory: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    problem: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str] = mapped_column(Text)
    steps: Mapped[str] = mapped_column(Text)
    prevention: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    source_ticket_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[Optional[list]] = mapped_column(Vector(1536).with_variant(JSON, "sqlite"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[Optional[str]] = mapped_column(String(255))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approval_job_id: Mapped[Optional[str]] = mapped_column(String(36))
    error_code: Mapped[Optional[str]] = mapped_column(String(64))

    def to_dict(self):
        return {
            "id": self.id, "category": self.category, "subcategory": self.subcategory,
            "title": self.title, "problem": self.problem, "root_cause": self.root_cause,
            "steps": self.steps, "prevention": self.prevention, "status": self.status,
            "source_ticket_ids": self.source_ticket_ids, "created_at": isoformat(self.created_at),
            "approved_by": self.approved_by, "approved_at": isoformat(self.approved_at),
            "error_code": self.error_code,
        }


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tickets_tenant_id"),
        UniqueConstraint("tenant_id", "created_by", "idempotency_key", name="uq_ticket_idempotency"),
        ForeignKeyConstraint(
            ["tenant_id", "runbook_id"], ["runbooks.tenant_id", "runbooks.id"],
            name="fk_ticket_runbook_tenant",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'recommended', 'escalated', 'resolved', 'failed')",
            name="ck_ticket_status",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_ticket_confidence"),
        Index("ix_tickets_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_tickets_tenant_pattern", "tenant_id", "category", "subcategory", "created_at"),
        Index("ix_tickets_tenant_actor", "tenant_id", "created_by", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    submitter: Mapped[str] = mapped_column(String(254))
    category_hint: Mapped[Optional[str]] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    category: Mapped[Optional[str]] = mapped_column(String(16))
    subcategory: Mapped[Optional[str]] = mapped_column(String(64))
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    recommendation: Mapped[Optional[str]] = mapped_column(Text)
    resolution: Mapped[Optional[str]] = mapped_column(Text)
    runbook_id: Mapped[Optional[str]] = mapped_column(String(36))
    assigned_to: Mapped[Optional[str]] = mapped_column(String(254))
    reason: Mapped[Optional[str]] = mapped_column(String(64))
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "submitter": self.submitter, "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at), "status": self.status,
            "category": self.category, "subcategory": self.subcategory,
            "confidence": self.confidence, "recommendation": self.recommendation, "resolution": self.resolution,
            "runbook_id": self.runbook_id, "assigned_to": self.assigned_to,
            "reason": self.reason, "error_code": self.error_code,
            "resolved_at": isoformat(self.resolved_at),
        }


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'retrying', 'completed', 'failed')", name="ck_job_status"),
        UniqueConstraint("tenant_id", "deduplication_key", name="uq_job_deduplication"),
        Index("ix_jobs_claim", "status", "available_at", "lease_expires_at"),
        Index("ix_jobs_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[Optional[str]] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(255))
    request_id: Mapped[str] = mapped_column(String(36))
    deduplication_key: Mapped[Optional[str]] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[Optional[str]] = mapped_column(String(36))
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def to_dict(self):
        return {
            "id": self.id, "kind": self.kind, "resource_id": self.resource_id,
            "status": self.status, "attempts": self.attempts,
            "error_code": self.last_error_code, "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
        }


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at", "id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[Optional[str]] = mapped_column(String(36))
    request_id: Mapped[str] = mapped_column(String(36))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {
            "id": self.id, "actor": self.actor, "action": self.action,
            "resource_id": self.resource_id, "request_id": self.request_id,
            "details": self.details, "created_at": isoformat(self.created_at),
        }

"""Tenant-owned ticketing, durable jobs, and approved vector runbooks."""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0001_enterprise"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("escalation_target", sa.String(254), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "runbooks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("subcategory", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("problem", sa.Text, nullable=False),
        sa.Column("root_cause", sa.Text, nullable=False),
        sa.Column("steps", sa.Text, nullable=False),
        sa.Column("prevention", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_ticket_ids", sa.JSON, nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(1536)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(255)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("approval_job_id", sa.String(36)),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("tenant_id", "id", name="uq_runbooks_tenant_id"),
        sa.UniqueConstraint("tenant_id", "source_fingerprint", name="uq_runbooks_source"),
        sa.CheckConstraint("status IN ('draft', 'approving', 'approved', 'rejected')", name="ck_runbook_status"),
    )
    op.create_index("ix_runbooks_tenant_pattern", "runbooks", ["tenant_id", "category", "subcategory", "status"])
    op.execute(
        "CREATE INDEX ix_runbooks_embedding ON runbooks USING hnsw (embedding vector_cosine_ops) "
        "WHERE status = 'approved'"
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("submitter", sa.String(254), nullable=False),
        sa.Column("category_hint", sa.String(16)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("category", sa.String(16)),
        sa.Column("subcategory", sa.String(64)),
        sa.Column("confidence", sa.Float),
        sa.Column("recommendation", sa.Text),
        sa.Column("resolution", sa.Text),
        sa.Column("runbook_id", sa.String(36)),
        sa.Column("assigned_to", sa.String(254)),
        sa.Column("reason", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tickets_tenant_id"),
        sa.UniqueConstraint("tenant_id", "created_by", "idempotency_key", name="uq_ticket_idempotency"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "runbook_id"], ["runbooks.tenant_id", "runbooks.id"], name="fk_ticket_runbook_tenant",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'recommended', 'escalated', 'resolved', 'failed')",
            name="ck_ticket_status",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_ticket_confidence"),
    )
    op.create_index("ix_tickets_tenant_created", "tickets", ["tenant_id", "created_at", "id"])
    op.create_index("ix_tickets_tenant_pattern", "tickets", ["tenant_id", "category", "subcategory", "created_at"])
    op.create_index("ix_tickets_tenant_actor", "tickets", ["tenant_id", "created_by", "created_at"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(36)),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("deduplication_key", sa.String(160)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'running', 'retrying', 'completed', 'failed')", name="ck_job_status"),
        sa.UniqueConstraint("tenant_id", "deduplication_key", name="uq_job_deduplication"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "available_at", "lease_expires_at"])
    op.create_index("ix_jobs_tenant_created", "jobs", ["tenant_id", "created_at"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(36)),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_tenant_created", "audit_events", ["tenant_id", "created_at", "id"])
    # Jobs contain dispatch metadata, not ticket bodies. Workers must claim across tenants.
    # Business records are additionally protected by PostgreSQL, even if a query omits a filter.
    for table in ("tickets", "runbooks", "audit_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))"
        )


def downgrade():
    for table in ("audit_events", "jobs", "tickets", "runbooks", "tenants"):
        op.drop_table(table)

from sqlalchemy import select

from app.errors import APIError
from app.ingest import source_fingerprint
from app.models import Ticket, utcnow
from datetime import timedelta


def source_tickets(session, tenant_id, category, subcategory, days=30):
    tickets = session.scalars(
        select(Ticket).where(
            Ticket.tenant_id == tenant_id,
            Ticket.category == category, Ticket.subcategory == subcategory,
            Ticket.created_at >= utcnow() - timedelta(days=days),
        ).order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(10)
    ).all()
    if len(tickets) < 3:
        raise APIError(409, "insufficient_pattern", "At least three matching tickets in the last 30 days are required.")
    ids = [ticket.id for ticket in tickets]
    return ids, source_fingerprint(category, subcategory, ids)

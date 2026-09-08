from app.db import audit
from app.errors import APIError
from app.models import utcnow


def confirm_resolution(session, ticket, resolution, actor, request_id):
    if ticket.status == "resolved":
        if ticket.resolution == resolution:
            return
        raise APIError(409, "already_resolved", "This ticket already has a confirmed resolution.")
    ticket.status = "resolved"
    ticket.resolution = resolution
    ticket.resolved_at = utcnow()
    ticket.error_code = None
    audit(session, ticket.tenant_id, actor, "ticket_resolved", ticket.id, request_id)

from datetime import timedelta

from sqlalchemy import and_, case, func, select

from app.models import Runbook, Ticket, isoformat, utcnow


def recurring_incidents(session, tenant_id, days=14, min_tickets=3, limit=20, now=None):
    now = now or utcnow()
    start, previous_start = now - timedelta(days=days), now - timedelta(days=days * 2)
    current = Ticket.created_at >= start
    total = func.sum(case((current, 1), else_=0))
    previous = func.sum(case((Ticket.created_at < start, 1), else_=0))
    unresolved = func.sum(case((and_(current, Ticket.status != "resolved"), 1), else_=0))
    escalations = func.sum(case((and_(current, Ticket.status.in_(("escalated", "failed"))), 1), else_=0))
    increase = case((total > previous, total - previous), else_=0)
    priority = unresolved * 3 + increase * 2 + total
    rows = session.execute(
        select(
            Ticket.category, Ticket.subcategory, total.label("ticket_count"),
            previous.label("previous_count"), unresolved.label("unresolved_count"),
            escalations.label("escalation_count"), priority.label("priority_score"),
        ).where(
            Ticket.tenant_id == tenant_id, Ticket.created_at >= previous_start,
            Ticket.created_at <= now, Ticket.category.is_not(None), Ticket.subcategory.is_not(None),
        ).group_by(Ticket.category, Ticket.subcategory)
        .having(total >= min_tickets).order_by(priority.desc(), total.desc(), Ticket.category, Ticket.subcategory)
        .limit(limit)
    ).all()
    coverage = {
        (category, subcategory, status): count
        for category, subcategory, status, count in session.execute(
            select(Runbook.category, Runbook.subcategory, Runbook.status, func.count()).where(
                Runbook.tenant_id == tenant_id, Runbook.status.in_(("approved", "draft", "approving")),
            ).group_by(Runbook.category, Runbook.subcategory, Runbook.status)
        )
    }
    issues = []
    for row in rows:
        approved = coverage.get((row.category, row.subcategory, "approved"), 0)
        drafts = (
            coverage.get((row.category, row.subcategory, "draft"), 0)
            + coverage.get((row.category, row.subcategory, "approving"), 0)
        )
        source_ids = session.scalars(
            select(Ticket.id).where(
                Ticket.tenant_id == tenant_id, Ticket.category == row.category,
                Ticket.subcategory == row.subcategory, Ticket.created_at >= start,
                Ticket.created_at <= now,
            ).order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(5)
        ).all()
        if approved:
            action = "Investigate recurrence and review the effectiveness of the approved runbook."
        elif drafts:
            action = "Review the existing draft before enabling recommendations."
        else:
            action = "Draft a runbook for human review to address this knowledge gap."
        issues.append({
            "category": row.category, "subcategory": row.subcategory,
            "ticket_count": row.ticket_count, "previous_count": row.previous_count,
            "unresolved_count": row.unresolved_count, "escalation_count": row.escalation_count,
            "escalation_rate": round(row.escalation_count / row.ticket_count, 4),
            "change_percent": round((row.ticket_count - row.previous_count) / row.previous_count * 100, 1)
            if row.previous_count else None,
            "priority_score": row.priority_score, "knowledge_gap": approved == 0,
            "approved_runbook_count": approved, "draft_runbook_count": drafts,
            "recommended_action": action, "source_ticket_ids": list(source_ids),
        })
    return {
        "window": {
            "days": days, "start": isoformat(start), "end": isoformat(now),
            "previous_start": isoformat(previous_start), "min_tickets": min_tickets,
        },
        "recurring_issues": issues,
    }

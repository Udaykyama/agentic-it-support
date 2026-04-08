from app.db import update_ticket

def auto_resolve(ticket, runbook):
    resolution = f"Auto-resolved using runbook: {runbook['title']}\n\n{runbook['steps']}"
    update_ticket(
        ticket.id,
        status="resolved",
        category=ticket.category,
        resolution=resolution
    )
    return {
        "action": "auto_resolved",
        "ticket_id": ticket.id,
        "runbook_used": runbook["title"],
        "resolution_summary": runbook["steps"][:200]
    }
from app.runbook_kb import search_runbook
from app.resolver import auto_resolve

CONFIDENCE_THRESHOLD = 0.75

def route_ticket(ticket, classification):
    confidence = classification.get("confidence", 0)
    subcategory = classification.get("subcategory", "")

    if confidence >= CONFIDENCE_THRESHOLD:
        runbook = search_runbook(subcategory, ticket.description)
        if runbook:
            return auto_resolve(ticket, runbook)

    return {
        "action": "escalated",
        "ticket_id": ticket.id,
        "reason": "low confidence or no runbook match",
        "category": ticket.category,
        "subcategory": subcategory,
        "assigned_to": "tier2-engineer@company.com"
    }
def route_ticket(classification, runbook, confidence_threshold, escalation_target):
    result = {
        "category": classification.category, "subcategory": classification.subcategory,
        "confidence": classification.confidence, "error_code": None,
    }
    if classification.confidence >= confidence_threshold and runbook is not None:
        return {
            **result, "status": "recommended", "runbook_id": runbook.id,
            "recommendation": runbook.steps, "assigned_to": None,
            "reason": "approved_runbook_match",
        }
    return {
        **result, "status": "escalated", "runbook_id": None, "recommendation": None,
        "assigned_to": escalation_target,
        "reason": "low_confidence" if classification.confidence < confidence_threshold else "no_relevant_runbook",
    }

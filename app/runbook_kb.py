from sqlalchemy import select

from app.models import Runbook


def has_approved_runbook(session, tenant_id, category):
    return session.scalar(
        select(Runbook.id).where(
            Runbook.tenant_id == tenant_id, Runbook.category == category,
            Runbook.status == "approved", Runbook.embedding.is_not(None),
        ).limit(1)
    ) is not None


def search_runbook(session, tenant_id, category, embedding, similarity_threshold):
    distance = Runbook.embedding.cosine_distance(embedding)
    return session.scalar(
        select(Runbook).where(
            Runbook.tenant_id == tenant_id, Runbook.category == category,
            Runbook.status == "approved", Runbook.embedding.is_not(None),
            distance <= 1 - similarity_threshold,
        ).order_by(distance, Runbook.id).limit(1).with_for_update()
    )

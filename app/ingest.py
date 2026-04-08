import uuid
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Ticket:
    title: str
    description: str
    submitter: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    status: str = "pending"
    category: Optional[str] = None
    category_hint: Optional[str] = None

def validate_and_normalize(raw: dict) -> Ticket:
    required = ["title", "description", "submitter"]
    for f in required:
        if not raw.get(f):
            raise ValueError(f"Missing required field: {f}")
    return Ticket(
        title=raw["title"].strip(),
        description=raw["description"].strip(),
        submitter=raw["submitter"].strip().lower(),
        category_hint=raw.get("category_hint")
    )
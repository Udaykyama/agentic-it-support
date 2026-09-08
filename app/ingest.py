from __future__ import annotations

import hashlib
import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

CATEGORIES = ("access", "hardware", "software", "network", "other")
Category = Literal["access", "hardware", "software", "network", "other"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TicketInput(InputModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=8000)
    submitter: EmailStr = Field(max_length=254)
    category_hint: Optional[Category] = None

    @field_validator("submitter")
    @classmethod
    def normalize_email(cls, value):
        return value.lower()


class Classification(InputModel):
    category: Category
    subcategory: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)


class RunbookDraft(InputModel):
    title: str = Field(min_length=1, max_length=200)
    problem: str = Field(min_length=1, max_length=2000)
    root_cause: str = Field(min_length=1, max_length=2000)
    steps: str = Field(min_length=1, max_length=12000)
    prevention: str = Field(min_length=1, max_length=2000)


class GenerateInput(InputModel):
    category: Category
    subcategory: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class ResolutionInput(InputModel):
    resolution: str = Field(min_length=1, max_length=12000)


def request_fingerprint(ticket):
    payload = json.dumps(ticket.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_fingerprint(category, subcategory, ticket_ids):
    payload = json.dumps([category, subcategory, sorted(ticket_ids)], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_and_normalize(raw):
    from app.errors import validate_input

    return validate_input(TicketInput, raw)

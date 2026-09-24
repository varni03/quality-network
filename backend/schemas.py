"""Request/response schemas."""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    author: str = "Analyst"


class NoteOut(BaseModel):
    id: int
    author: str
    body: str
    created_at: datetime

    class Config:
        from_attributes = True


class DefectUpdate(BaseModel):
    status: Optional[str] = None      # New | Reviewed | Resolved
    flagged: Optional[bool] = None


class DefectOut(BaseModel):
    id: int
    inspection_id: str
    vin: str
    make: str
    model: str
    year: str
    inspected_at: str
    part_name: str
    module: str
    description: str
    cost: float
    status: str
    flagged: bool
    notes: List[NoteOut] = []

    class Config:
        from_attributes = True


class AskRequest(BaseModel):
    question: str
    source: str | None = None

"""User proxy to public.users (placeholder for Phase 4+ auth)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class User:
    id: int
    email: str
    display_name: str
    role: str
    is_active: bool

    @classmethod
    def from_row(cls, row: Any) -> "User":
        return cls(
            id=row[0], email=row[1], display_name=row[3], role=row[4], is_active=row[5],
        )

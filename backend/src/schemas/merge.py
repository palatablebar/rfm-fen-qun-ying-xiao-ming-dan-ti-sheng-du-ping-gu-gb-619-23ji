from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MergeJobCreateIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class MergeJobQuery(BaseModel):
    status: Literal["pending", "running", "completed", "failed"] | None = None
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class MergeRecordQuery(BaseModel):
    job_id: int | None = Field(default=None, ge=1)
    master_member_id: int | None = Field(default=None, ge=1)
    match_kind: Literal["phone", "external"] | None = None
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

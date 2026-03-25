from pydantic import BaseModel, field_validator
from typing import Optional, List
from datetime import datetime


# ── Request Schemas ──

class DomainCreateRequest(BaseModel):
    domains: list[str]

    @field_validator("domains", mode="before")
    @classmethod
    def parse_domains(cls, v):
        if isinstance(v, str):
            cleaned = v.replace(",", "\n")
            return [d.strip().lower() for d in cleaned.split("\n") if d.strip()]
        return [d.strip().lower() for d in v if d.strip()]

class BulkFetchRequest(BaseModel):
    domain_ids: list[str]


class PageFilterParams(BaseModel):
    page: int = 1
    limit: int = 50
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    search: Optional[str] = None


# ── Response Schemas ──

class DomainResponse(BaseModel):
    id: str
    domain: str
    status: str
    total_pages: int
    last_fetched_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    live_status: Optional[str] = None
    live_status_code: Optional[int] = None
    live_final_url: Optional[str] = None
    naman_approved: bool = False
    harsha_approved: bool = False

    @field_validator("naman_approved", "harsha_approved", mode="before")
    @classmethod
    def coerce_bool(cls, v):
        if v is None:
            return False
        return bool(v)

    model_config = {"from_attributes": True}


class PageResponse(BaseModel):
    id: str
    domain_id: str
    original_url: str
    urlkey: str
    timestamp: str
    wayback_url: str
    status_code: Optional[str] = None
    mimetype: Optional[str] = None
    digest: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PaginatedPagesResponse(BaseModel):
    pages: List[PageResponse]
    total: int
    page: int
    limit: int
    total_pages: int


class FetchJobResponse(BaseModel):
    id: str
    domain_id: str
    status: str
    pages_found: int
    error_msg: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ApprovalRequest(BaseModel):
    approver: str  # "naman" or "harsha"
    approved: bool = True


class BulkDomainResponse(BaseModel):
    added: List[DomainResponse]
    skipped: List[str]

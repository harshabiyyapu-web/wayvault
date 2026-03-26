import math
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Domain, Page
from app.schemas import PageResponse, PaginatedPagesResponse
from app.services.csv_export import generate_csv

router = APIRouter(prefix="/api/domains/{domain_id}", tags=["pages"])


@router.get("/pages", response_model=PaginatedPagesResponse)
async def list_pages(
    domain_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    date_from: Optional[str] = Query(None, description="YYYYMMDD format"),
    date_to: Optional[str] = Query(None, description="YYYYMMDD format"),
    search: Optional[str] = Query(None, description="Keyword search in URL"),
    db: AsyncSession = Depends(get_db),
):
    """Get paginated list of archived pages for a domain, with optional filters."""
    domain = await db.get(Domain, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    # Build query filters
    filters = [Page.domain_id == domain_id]

    if date_from:
        filters.append(Page.timestamp >= date_from)
    if date_to:
        # Pad to end of day
        filters.append(Page.timestamp <= date_to + "235959")
    if search:
        filters.append(Page.original_url.ilike(f"%{search}%"))

    where_clause = and_(*filters)

    # Get total count
    count_result = await db.execute(
        select(func.count(Page.id)).where(where_clause)
    )
    total = count_result.scalar() or 0

    # Get paginated results
    offset = (page - 1) * limit
    result = await db.execute(
        select(Page)
        .where(where_clause)
        .order_by(Page.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    pages_list = result.scalars().all()

    return PaginatedPagesResponse(
        pages=[PageResponse.model_validate(p) for p in pages_list],
        total=total,
        page=page,
        limit=limit,
        total_pages=math.ceil(total / limit) if total > 0 else 0,
    )


@router.get("/export")
async def export_csv(domain_id: str, db: AsyncSession = Depends(get_db)):
    """Export all pages for a domain as CSV."""
    domain = await db.get(Domain, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    csv_content = await generate_csv(db, domain_id)

    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={domain.domain}_wayback.csv"
        },
    )

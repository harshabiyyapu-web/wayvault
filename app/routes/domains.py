import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.database import get_db
from app.models import Domain, Page, FetchJob
from app.schemas import DomainCreateRequest, DomainResponse, BulkDomainResponse, FetchJobResponse, BulkFetchRequest
from app.worker import run_fetch_job, job_progress, enqueue_fetch

router = APIRouter(prefix="/api/domains", tags=["domains"])


@router.post("/bulk-fetch")
async def bulk_fetch(
    req: BulkFetchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Enqueue multiple fetch jobs safely."""
    queued = []

    for domain_id in req.domain_ids:
        domain = await db.get(Domain, domain_id)
        if domain and domain.status not in ("fetching", "pending"):
            domain.status = "pending"
            enqueue_fetch(domain.id, domain.domain)
            queued.append(domain.id)

    await db.commit()

    return {"detail": f"Queued {len(queued)} domains for fetch", "queued_ids": queued}


@router.post("", response_model=BulkDomainResponse)
async def add_domains(req: DomainCreateRequest, db: AsyncSession = Depends(get_db)):
    """Add one or multiple domains."""
    added = []
    skipped = []

    for domain_name in req.domains:
        # Normalize: strip protocol and trailing slashes
        clean = domain_name.strip().lower()
        for prefix in ["https://", "http://", "www."]:
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
        clean = clean.rstrip("/")

        if not clean:
            continue

        # Check if already exists
        existing = await db.execute(select(Domain).where(Domain.domain == clean))
        if existing.scalar_one_or_none():
            skipped.append(clean)
            continue

        domain = Domain(domain=clean)
        db.add(domain)
        await db.commit()
        await db.refresh(domain)
        added.append(DomainResponse.model_validate(domain))

    return BulkDomainResponse(added=added, skipped=skipped)


@router.get("", response_model=list[DomainResponse])
async def list_domains(db: AsyncSession = Depends(get_db)):
    """List all domains with status info."""
    result = await db.execute(
        select(Domain).order_by(Domain.created_at.desc())
    )
    domains = result.scalars().all()
    return [DomainResponse.model_validate(d) for d in domains]


@router.get("/{domain_id}", response_model=DomainResponse)
async def get_domain(domain_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single domain by ID."""
    domain = await db.get(Domain, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return DomainResponse.model_validate(domain)


@router.delete("/{domain_id}")
async def delete_domain(domain_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a domain and all its pages."""
    domain = await db.get(Domain, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    await db.delete(domain)
    await db.commit()
    return {"detail": "Domain deleted", "domain": domain.domain}


@router.post("/{domain_id}/fetch")
async def trigger_fetch(
    domain_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Enqueue a Wayback CDX fetch job for a domain."""
    domain = await db.get(Domain, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    
    if domain.status in ("fetching", "pending"):
        raise HTTPException(status_code=409, detail="Fetch already in progress or queued")

    domain.status = "pending"
    await db.commit()

    enqueue_fetch(domain.id, domain.domain)

    return {"detail": "Fetch job queued", "domain_id": domain.id, "domain": domain.domain}

@router.get("/{domain_id}/status")
async def fetch_status_sse(domain_id: str, db: AsyncSession = Depends(get_db)):
    """SSE endpoint streaming fetch job progress."""
    domain = await db.get(Domain, domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    async def event_generator():
        while True:
            progress = job_progress.get(domain_id, {
                "status": domain.status,
                "pages_found": domain.total_pages,
                "message": "No active job",
            })

            yield {"data": json.dumps(progress)}

            if progress.get("status") in ("done", "failed"):
                break

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


@router.get("/{domain_id}/jobs", response_model=list[FetchJobResponse])
async def list_fetch_jobs(domain_id: str, db: AsyncSession = Depends(get_db)):
    """List all fetch jobs for a domain."""
    result = await db.execute(
        select(FetchJob)
        .where(FetchJob.domain_id == domain_id)
        .order_by(FetchJob.started_at.desc())
    )
    jobs = result.scalars().all()
    return [FetchJobResponse.model_validate(j) for j in jobs]

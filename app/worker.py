import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Domain, Page, FetchJob
from app.services.cdx_fetcher import fetch_homepage_snapshots, check_domain_status
from app.database import async_session

logger = logging.getLogger("wayvault.worker")

# In-memory job progress tracker (for SSE)
job_progress: dict[str, dict] = {}

# Background queue to process fetches one by one to avoid rate limits
fetch_queue = asyncio.Queue()

async def fetch_worker_loop():
    logger.info("Starting background fetch worker loop...")
    while True:
        domain_id, domain_name = await fetch_queue.get()
        try:
            logger.info(f"Worker processing domain: {domain_name}")
            await run_fetch_job(domain_id, domain_name)
        except Exception as e:
            logger.error(f"Worker queue error for {domain_name}: {e}")
        finally:
            fetch_queue.task_done()
            await asyncio.sleep(3) # mandatory 3 second delay between domains to respect Wayback

def enqueue_fetch(domain_id: str, domain_name: str):
    # Set status to pending so UI knows it's queued
    job_progress[domain_id] = {
        "job_id": None,
        "status": "pending",
        "pages_found": 0,
        "message": "Queued for fetching...",
    }
    fetch_queue.put_nowait((domain_id, domain_name))


async def run_fetch_job(domain_id: str, domain_name: str):
    """
    Background task:
    1. Check live status of the domain homepage
    2. Fetch all unique homepage snapshots from Wayback CDX API
    3. Store results in DB
    """
    job_id = None

    try:
        async with async_session() as db:
            # Create fetch job record
            job = FetchJob(
                domain_id=domain_id,
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)
            job_id = job.id

            # Update domain status
            domain = await db.get(Domain, domain_id)
            if domain:
                domain.status = "fetching"
                await db.commit()

        # Step 1: Check live domain status
        job_progress[domain_id] = {
            "job_id": job_id,
            "status": "running",
            "pages_found": 0,
            "message": f"Checking live status of {domain_name}...",
        }

        live_status = await check_domain_status(domain_name)

        # Save live status to domain
        async with async_session() as db:
            domain = await db.get(Domain, domain_id)
            if domain:
                domain.live_status = live_status.get("live_status", "unknown")
                domain.live_status_code = live_status.get("status_code")
                domain.live_final_url = live_status.get("final_url")
                await db.commit()

        job_progress[domain_id] = {
            "job_id": job_id,
            "status": "running",
            "pages_found": 0,
            "message": f"Live: {live_status['live_status'].upper()} (HTTP {live_status.get('status_code', '?')}). Now fetching Wayback snapshots...",
        }

        # Step 2: Fetch unique homepage snapshots
        def on_progress(pages_so_far, message):
            job_progress[domain_id] = {
                "job_id": job_id,
                "status": "running",
                "pages_found": pages_so_far,
                "message": message,
            }

        snapshots = await fetch_homepage_snapshots(domain_name, progress_callback=on_progress)

        # Step 2b: Deduplicate by digest (keep the oldest snapshot for each unique content version)
        unique_snapshots = []
        seen_digests = set()
        
        # Ensure ordered oldest-to-newest
        snapshots.sort(key=lambda x: x["timestamp"])
        
        for record in snapshots:
            digest = record.get("digest")
            if not digest or digest not in seen_digests:
                unique_snapshots.append(record)
                if digest:
                    seen_digests.add(digest)
                    
        snapshots = unique_snapshots

        # Step 3: Save to DB
        async with async_session() as db:
            # Clear existing pages for this domain (re-fetch support)
            await db.execute(delete(Page).where(Page.domain_id == domain_id))
            await db.commit()

            job_progress[domain_id] = {
                "job_id": job_id,
                "status": "running",
                "pages_found": len(snapshots),
                "message": f"Saving {len(snapshots)} snapshots to database...",
            }

            # Bulk insert
            batch_size = 500
            for i in range(0, len(snapshots), batch_size):
                batch = snapshots[i : i + batch_size]
                for record in batch:
                    page = Page(
                        domain_id=domain_id,
                        original_url=record.get("original", domain_name),
                        urlkey=record.get("urlkey", ""),
                        timestamp=record["timestamp"],
                        wayback_url=record["wayback_url"],
                        status_code=record.get("statuscode"),
                        mimetype=record.get("mimetype"),
                        digest=record.get("digest"),
                    )
                    db.add(page)
                await db.commit()

            # Update domain
            domain = await db.get(Domain, domain_id)
            if domain:
                domain.status = "done"
                domain.total_pages = len(snapshots)
                domain.last_fetched_at = datetime.now(timezone.utc)
                await db.commit()

            # Update fetch job
            fetch_job = await db.get(FetchJob, job_id)
            if fetch_job:
                fetch_job.status = "done"
                fetch_job.pages_found = len(snapshots)
                fetch_job.finished_at = datetime.now(timezone.utc)
                await db.commit()

        job_progress[domain_id] = {
            "job_id": job_id,
            "status": "done",
            "pages_found": len(snapshots),
            "message": f"✅ Done! {len(snapshots)} unique homepage snapshots. Live: {live_status['live_status'].upper()}",
        }

        logger.info(f"Fetch complete for {domain_name}: {len(snapshots)} snapshots, live={live_status['live_status']}")

    except Exception as e:
        logger.error(f"Fetch job failed for {domain_name}: {e}")

        job_progress[domain_id] = {
            "job_id": job_id,
            "status": "failed",
            "pages_found": 0,
            "message": f"❌ Error: {str(e)}",
        }

        try:
            async with async_session() as db:
                domain = await db.get(Domain, domain_id)
                if domain:
                    domain.status = "error"
                    await db.commit()

                if job_id:
                    fetch_job = await db.get(FetchJob, job_id)
                    if fetch_job:
                        fetch_job.status = "failed"
                        fetch_job.error_msg = str(e)[:1000]
                        fetch_job.finished_at = datetime.now(timezone.utc)
                        await db.commit()
        except Exception as db_err:
            logger.error(f"Failed to update error status: {db_err}")

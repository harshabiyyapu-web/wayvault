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

        # Detect if the domain redirects to a same-domain path (e.g. example.com → example.com/home)
        # If so, we'll also query CDX for that path to catch snapshots that were only archived there.
        redirect_url = None
        final_url = live_status.get("final_url")
        if final_url and live_status.get("live_status") != "redirected_away":
            from urllib.parse import urlparse
            parsed = urlparse(final_url)
            final_domain = parsed.netloc.replace("www.", "")
            if final_domain == domain_name or final_domain == f"www.{domain_name}":
                redirect_path = parsed.path.strip("/")
                if redirect_path and redirect_path.lower() not in ("", "index.html", "index.htm", "index.php"):
                    redirect_url = f"{domain_name}/{redirect_path}"
                    logger.info(f"{domain_name}: detected same-domain redirect to path /{redirect_path}, will also query CDX for {redirect_url}")

        # Step 2: Fetch unique homepage snapshots
        def on_progress(pages_so_far, message):
            job_progress[domain_id] = {
                "job_id": job_id,
                "status": "running",
                "pages_found": pages_so_far,
                "message": message,
            }

        snapshots = await fetch_homepage_snapshots(domain_name, progress_callback=on_progress)

        # Step 2c: If a redirect path was detected, fetch CDX for it too and merge
        if redirect_url:
            job_progress[domain_id] = {
                "job_id": job_id,
                "status": "running",
                "pages_found": len(snapshots),
                "message": f"Also checking redirect path /{redirect_url.split('/', 1)[-1]} for more snapshots...",
            }
            try:
                redirect_snapshots = await fetch_homepage_snapshots(redirect_url, progress_callback=None)
                if redirect_snapshots:
                    existing_digests = {s.get("digest") for s in snapshots if s.get("digest")}
                    merged = 0
                    for s in redirect_snapshots:
                        if s.get("digest") not in existing_digests:
                            snapshots.append(s)
                            if s.get("digest"):
                                existing_digests.add(s.get("digest"))
                            merged += 1
                    if merged:
                        logger.info(f"{domain_name}: merged {merged} extra snapshots from redirect path {redirect_url}")
            except Exception as e:
                logger.warning(f"{domain_name}: redirect path CDX query failed (non-fatal): {e}")

        # Step 2b: Filter out records missing required fields, then deduplicate by digest
        valid_snapshots = []
        for r in snapshots:
            ts = r.get("timestamp")
            orig = r.get("original")
            wurl = r.get("wayback_url")
            if ts and orig and wurl:
                valid_snapshots.append(r)
            else:
                logger.warning(f"Skipping CDX record with missing fields for {domain_name}: {r}")

        # Sort oldest-to-newest and deduplicate
        valid_snapshots.sort(key=lambda x: x.get("timestamp", ""))
        seen_digests: set = set()
        unique_snapshots = []
        for record in valid_snapshots:
            digest = record.get("digest")
            if not digest or digest not in seen_digests:
                unique_snapshots.append(record)
                if digest:
                    seen_digests.add(digest)
        snapshots = unique_snapshots

        logger.info(f"{domain_name}: {len(snapshots)} valid unique snapshots to save")

        # Step 3: Save to DB — single transaction so delete + inserts are atomic.
        # Uses flush() per batch (writes to DB without committing) then one final
        # commit. If anything fails the session auto-rolls back, preserving old data.
        async with async_session() as db:
            job_progress[domain_id] = {
                "job_id": job_id,
                "status": "running",
                "pages_found": len(snapshots),
                "message": f"Saving {len(snapshots)} snapshots to database...",
            }

            # Delete old pages inside the same transaction
            await db.execute(delete(Page).where(Page.domain_id == domain_id))

            # Insert new pages in batches, flushing (not committing) between batches
            batch_size = 500
            for i in range(0, len(snapshots), batch_size):
                batch = snapshots[i : i + batch_size]
                for record in batch:
                    page = Page(
                        domain_id=domain_id,
                        original_url=record.get("original") or domain_name,
                        urlkey=record.get("urlkey") or "",
                        timestamp=record.get("timestamp") or "",
                        wayback_url=record.get("wayback_url") or "",
                        status_code=record.get("statuscode"),
                        mimetype=record.get("mimetype"),
                        digest=record.get("digest"),
                    )
                    db.add(page)
                await db.flush()  # write batch to DB, still inside transaction

            # Update domain and job inside the same transaction
            domain = await db.get(Domain, domain_id)
            if domain:
                domain.status = "done"
                domain.total_pages = len(snapshots)
                domain.last_fetched_at = datetime.now(timezone.utc)

            fetch_job = await db.get(FetchJob, job_id)
            if fetch_job:
                fetch_job.status = "done"
                fetch_job.pages_found = len(snapshots)
                fetch_job.finished_at = datetime.now(timezone.utc)

            await db.commit()  # single commit — everything or nothing

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

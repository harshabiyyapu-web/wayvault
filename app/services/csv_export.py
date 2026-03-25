import csv
import io
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Page


async def generate_csv(db: AsyncSession, domain_id: str) -> str:
    """Generate CSV string for all pages of a domain."""
    result = await db.execute(
        select(Page)
        .where(Page.domain_id == domain_id)
        .order_by(Page.timestamp.asc())
    )
    pages = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["url", "timestamp", "wayback_link", "status_code", "mimetype", "digest"])

    for page in pages:
        writer.writerow([
            page.original_url,
            page.timestamp,
            page.wayback_url,
            page.status_code,
            page.mimetype,
            page.digest,
        ])

    return output.getvalue()

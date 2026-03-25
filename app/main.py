import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import asyncio
from sqlalchemy import text, update
from app.config import CORS_ORIGINS
from app.database import init_db, async_session
from app.models import Domain
from app.routes import domains, pages, preview
from app.worker import fetch_worker_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Auto-migrate: add any missing columns to the domains table
    async with async_session() as db:
        migrations = [
            "ALTER TABLE domains ADD COLUMN live_status TEXT",
            "ALTER TABLE domains ADD COLUMN live_status_code INTEGER",
            "ALTER TABLE domains ADD COLUMN live_final_url TEXT",
            "ALTER TABLE domains ADD COLUMN naman_approved BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE domains ADD COLUMN harsha_approved BOOLEAN NOT NULL DEFAULT 0",
        ]
        for stmt in migrations:
            try:
                await db.execute(text(stmt))
                await db.commit()
            except Exception:
                await db.rollback()  # column already exists — skip silently

    # Reset any stalled fetch jobs on startup
    async with async_session() as db:
        await db.execute(
            update(Domain).where(Domain.status.in_(["fetching", "pending"])).values(status="error")
        )
        await db.commit()

    asyncio.create_task(fetch_worker_loop())
    yield


app = FastAPI(
    title="WayVault API",
    description="Wayback Machine Intelligence Dashboard — Backend API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(domains.router)
app.include_router(pages.router)
app.include_router(preview.router)

# Serve static frontend
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_frontend():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "wayvault"}

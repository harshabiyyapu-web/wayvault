import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./wayvault.db")

CDX_BASE_URL = "https://web.archive.org/cdx/search/cdx"
CDX_RATE_LIMIT_SECONDS = 1.0
CDX_USER_AGENT = "WayVault/1.0 (self-hosted dashboard)"
CDX_PAGE_LIMIT = 100000

CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://wayback.185.187.170.104.sslip.io",
    "http://wayback.185.187.170.104.sslip.io",
]

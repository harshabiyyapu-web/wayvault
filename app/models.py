import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


def generate_uuid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


class Domain(Base):
    __tablename__ = "domains"

    id = Column(String, primary_key=True, default=generate_uuid)
    domain = Column(String, unique=True, nullable=False, index=True)
    status = Column(String, default="pending")  # pending | fetching | done | error
    total_pages = Column(Integer, default=0)
    last_fetched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    # Live status fields
    live_status = Column(String, nullable=True)       # ok | redirect | unreachable | timeout | error ...
    live_status_code = Column(Integer, nullable=True)  # HTTP status code
    live_final_url = Column(String, nullable=True)     # Final URL after redirects

    pages = relationship("Page", back_populates="domain_rel", cascade="all, delete-orphan")
    fetch_jobs = relationship("FetchJob", back_populates="domain_rel", cascade="all, delete-orphan")


class Page(Base):
    __tablename__ = "pages"

    id = Column(String, primary_key=True, default=generate_uuid)
    domain_id = Column(String, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True)
    original_url = Column(Text, nullable=False)
    urlkey = Column(String, nullable=False, index=True)
    timestamp = Column(String, nullable=False)  # YYYYMMDDHHMMSS from CDX
    wayback_url = Column(Text, nullable=False)
    status_code = Column(String, nullable=True)
    mimetype = Column(String, nullable=True)
    digest = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    domain_rel = relationship("Domain", back_populates="pages")

    __table_args__ = (
        UniqueConstraint("domain_id", "digest", name="uq_domain_digest"),
    )


class FetchJob(Base):
    __tablename__ = "fetch_jobs"

    id = Column(String, primary_key=True, default=generate_uuid)
    domain_id = Column(String, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, default="queued")  # queued | running | done | failed
    pages_found = Column(Integer, default=0)
    error_msg = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    domain_rel = relationship("Domain", back_populates="fetch_jobs")

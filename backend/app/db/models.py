from __future__ import annotations

import uuid
from datetime import datetime
from json import JSONDecodeError, loads

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def uuid_str() -> str:
    return str(uuid.uuid4())


class GroupSource(Base):
    __tablename__ = 'group_sources'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    @property
    def privacy(self) -> str | None:
        if not self.note:
            return None
        try:
            payload = loads(self.note)
        except (TypeError, JSONDecodeError):
            return None
        metadata = payload.get('facebook_group_metadata') if isinstance(payload, dict) else None
        if not isinstance(metadata, dict):
            return None
        value = str(metadata.get('privacy') or '').strip().lower()
        return value or None


class Keyword(Base):
    __tablename__ = 'keywords'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    keyword: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ScanRun(Base):
    __tablename__ = 'scan_runs'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default='running', nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    groups_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    groups_success: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    groups_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posts_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posts_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posts_matched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)

    posts: Mapped[list['ScrapedPost']] = relationship(back_populates='run')
    errors: Mapped[list['ErrorLog']] = relationship(back_populates='run')


class ScrapedPost(Base):
    __tablename__ = 'scraped_posts'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey('scan_runs.id', ondelete='SET NULL'))
    group_url: Mapped[str] = mapped_column(Text, nullable=False)
    group_name: Mapped[str | None] = mapped_column(String(255))
    post_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    post_url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    matched_keywords: Mapped[str | None] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    run: Mapped[ScanRun | None] = relationship(back_populates='posts')


class LeadCandidate(Base):
    __tablename__ = 'lead_candidates'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    post_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    group_name: Mapped[str | None] = mapped_column(String(255))
    group_url: Mapped[str | None] = mapped_column(Text)
    post_url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    need_stage: Mapped[str | None] = mapped_column(String(32))
    persona: Mapped[str | None] = mapped_column(String(255))
    pain_points: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    suggested_comment: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default='new', nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), default='facebook_group', nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ErrorLog(Base):
    __tablename__ = 'error_logs'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey('scan_runs.id', ondelete='SET NULL'))
    group_url: Mapped[str | None] = mapped_column(Text)
    engine: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run: Mapped[ScanRun | None] = relationship(back_populates='errors')


class AppSetting(Base):
    __tablename__ = 'app_settings'

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

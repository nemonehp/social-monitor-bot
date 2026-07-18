from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.enums import (
    CredentialPlatform,
    CredentialStatus,
    DeliveryStatus,
    ItemType,
    JobStatus,
    Platform,
    ProxyStatus,
    SourceStatus,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class AppSetting(Base, TimestampMixin):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class AllowedUser(Base, TimestampMixin):
    __tablename__ = "allowed_users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Source(Base, TimestampMixin):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("platform", "normalized_link", name="uq_sources_platform_link"),
        Index("ix_sources_due", "status", "next_check_at"),
        Index("ix_sources_region", "region"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    platform: Mapped[Platform] = mapped_column(Enum(Platform, name="platform_enum"), nullable=False)
    input_link: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_link: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    region: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    federal_district: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    status: Mapped[SourceStatus] = mapped_column(
        Enum(SourceStatus, name="source_status_enum"), default=SourceStatus.ACTIVE, nullable=False
    )
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    poll_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_item_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    last_error_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    state: Mapped[SourceState] = relationship(back_populates="source", uselist=False, cascade="all, delete-orphan")
    items: Mapped[list[Item]] = relationship(back_populates="source")


class SourceState(Base, TimestampMixin):
    __tablename__ = "source_states"

    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    monitor_from_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    checkpoint_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    bootstrap_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    post_watermark: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    story_watermark: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    post_cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    story_cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    recent_post_keys: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    recent_story_keys: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    source: Mapped[Source] = relationship(back_populates="state")


class CollectionJob(Base, TimestampMixin):
    __tablename__ = "collection_jobs"
    __table_args__ = (
        Index("ix_collection_jobs_claim", "platform", "status", "run_after"),
        Index("ix_collection_jobs_cleanup", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    platform: Mapped[Platform] = mapped_column(Enum(Platform, name="job_platform_enum"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status_enum"), default=JobStatus.PENDING, nullable=False
    )
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Item(Base, TimestampMixin):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("item_key", name="uq_items_item_key"),
        Index("ix_items_source_published", "source_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    platform: Mapped[Platform] = mapped_column(Enum(Platform, name="item_platform_enum"), nullable=False)
    item_type: Mapped[ItemType] = mapped_column(Enum(ItemType, name="item_type_enum"), nullable=False)
    item_key: Mapped[str] = mapped_column(String(500), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    original_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    source: Mapped[Source] = relationship(back_populates="items")
    media: Mapped[list[Media]] = relationship(back_populates="item", cascade="all, delete-orphan")
    deliveries: Mapped[list[Delivery]] = relationship(back_populates="item", cascade="all, delete-orphan")


class Media(Base, TimestampMixin):
    __tablename__ = "media"
    __table_args__ = (Index("ix_media_item_order", "item_id", "position"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    media_type: Mapped[str] = mapped_column(String(50), nullable=False)
    remote_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    preview_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    local_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    item: Mapped[Item] = relationship(back_populates="media")


class Delivery(Base, TimestampMixin):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("item_id", "target_chat_id", name="uq_delivery_item_chat"),
        Index("ix_deliveries_claim", "status", "run_after"),
        Index("ix_deliveries_cleanup", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    target_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status_enum"), default=DeliveryStatus.PENDING, nullable=False
    )
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    telegram_message_ids: Mapped[list[int]] = mapped_column(JSONB, default=list, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    item: Mapped[Item] = relationship(back_populates="deliveries")


class Credential(Base, TimestampMixin):
    __tablename__ = "credentials"
    __table_args__ = (
        UniqueConstraint("platform", "label", name="uq_credentials_platform_label"),
        Index("ix_credentials_available", "platform", "status", "cooldown_until"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    platform: Mapped[CredentialPlatform] = mapped_column(
        Enum(CredentialPlatform, name="credential_platform_enum"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[CredentialStatus] = mapped_column(
        Enum(CredentialStatus, name="credential_status_enum"), default=CredentialStatus.ACTIVE, nullable=False
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    requests_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class Proxy(Base, TimestampMixin):
    __tablename__ = "proxies"
    __table_args__ = (
        UniqueConstraint("canonical_url_hash", name="uq_proxies_url_hash"),
        Index("ix_proxies_available", "status", "quarantine_until"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    canonical_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proxy_url_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    display: Mapped[str] = mapped_column(String(500), nullable=False)
    scheme: Mapped[str] = mapped_column(String(20), nullable=False)
    country_code: Mapped[str] = mapped_column(String(10), default="", nullable=False)
    external_ip: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ProxyStatus] = mapped_column(
        Enum(ProxyStatus, name="proxy_status_enum"), default=ProxyStatus.HEALTHY, nullable=False
    )
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quarantine_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AlertState(Base, TimestampMixin):
    __tablename__ = "alert_states"

    alert_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

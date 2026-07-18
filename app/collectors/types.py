from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.db.enums import ItemType, Platform


@dataclass(slots=True)
class MediaPayload:
    media_type: str
    remote_url: str = ""
    preview_url: str = ""
    local_path: str = ""
    mime_type: str = ""
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CollectedItem:
    platform: Platform
    item_type: ItemType
    item_key: str
    external_id: str
    original_url: str
    text: str
    published_at: datetime | None
    is_pinned: bool = False
    media: list[MediaPayload] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CollectionResult:
    title: str = ""
    external_id: str = ""
    normalized_link: str = ""
    items: list[CollectedItem] = field(default_factory=list)
    post_watermark: str = ""
    story_watermark: str = ""
    post_cursor: dict[str, Any] = field(default_factory=dict)
    story_cursor: dict[str, Any] = field(default_factory=dict)
    window_start: datetime | None = None
    window_end: datetime | None = None
    needs_immediate_retry: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)

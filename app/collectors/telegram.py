from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from telethon import TelegramClient

from app.collectors.errors import (
    AccessDeniedError,
    CredentialDeadError,
    NetworkCollectorError,
    NotFoundError,
    RateLimitedError,
    RetryableCollectorError,
)
from app.collectors.types import CollectedItem, CollectionResult, MediaPayload
from app.config import Settings
from app.db.enums import ItemType, Platform
from app.db.models import Source
from app.services.image_preview import PreparedPreview, decorate_video_preview, prepare_preview
from app.utils.text import clean_text

BAD_SESSION_ERRORS = {
    "AuthKeyUnregisteredError",
    "AuthKeyInvalidError",
    "AuthKeyDuplicatedError",
    "SessionRevokedError",
    "SessionExpiredError",
    "SessionPasswordNeededError",
    "UserDeactivatedBanError",
    "UserDeactivatedError",
    "PhoneNumberBannedError",
}
NO_ACCESS_ERRORS = {
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "UserNotParticipantError",
    "InviteHashExpiredError",
    "InviteHashInvalidError",
    "ChatWriteForbiddenError",
}
NOT_FOUND_ERRORS = {"UsernameInvalidError", "UsernameNotOccupiedError", "UsernameNotModifiedError"}
CLIENT_RESTART_ERRORS = {
    "TypeNotFoundError",
    "InvalidBufferError",
}
RETRY_ERRORS = {
    "TimeoutError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "ConnectionRefusedError",
    "OSError",
    "IncompleteReadError",
    "ServerError",
    "TimedOutError",
    "RpcCallFailError",
    "PhoneMigrateError",
    "NetworkMigrateError",
    "FileMigrateError",
}


def _exc_name(exc: BaseException) -> str:
    return type(exc).__name__


def _safe_exception_text(exc: BaseException, *, limit: int = 500) -> str:
    name = _exc_name(exc)
    # Telethon's TypeNotFoundError may include the entire undecoded binary buffer
    # in __str__. That buffer is noisy and can contain session-adjacent payload
    # bytes, so never render it into logs or database error fields.
    if name in CLIENT_RESTART_ERRORS:
        return f"{name}: Telegram payload decode failed; client restart required"
    try:
        value = str(exc)
    except Exception:
        value = "unprintable exception"
    value = value.replace("\x00", "")
    if len(value) > limit:
        value = value[:limit].rstrip() + "…"
    return value


def _classify(exc: BaseException) -> Exception:
    name = _exc_name(exc)
    message = _safe_exception_text(exc)
    if name in BAD_SESSION_ERRORS:
        return CredentialDeadError(message)
    if name in NO_ACCESS_ERRORS:
        return AccessDeniedError(message)
    if name in NOT_FOUND_ERRORS:
        return NotFoundError(message)
    if name == "FloodWaitError":
        return RateLimitedError(message, retry_after=int(getattr(exc, "seconds", 60) or 60))
    if (
        name in RETRY_ERRORS
        or name in CLIENT_RESTART_ERRORS
        or isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError))
    ):
        return NetworkCollectorError(message)
    return RetryableCollectorError(f"{name}: {message}")


def _entity_type(entity: Any) -> str:
    name = type(entity).__name__
    if name == "User":
        return "bot" if bool(getattr(entity, "bot", False)) else "user"
    if name == "Channel":
        if bool(getattr(entity, "broadcast", False)):
            return "channel"
        if bool(getattr(entity, "megagroup", False)) or bool(getattr(entity, "gigagroup", False)):
            return "group"
        return "channel_or_group"
    if name == "Chat":
        return "group"
    return name.lower()


def _title(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if title:
        return clean_text(title)
    full = clean_text(f"{getattr(entity, 'first_name', '')} {getattr(entity, 'last_name', '')}")
    return full or clean_text(getattr(entity, "username", "")) or type(entity).__name__


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _jsonable(value: Any, depth: int = 5) -> Any:
    if depth <= 0:
        return type(value).__name__
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()[:512]
    if isinstance(value, datetime):
        converted = _utc(value)
        return converted.isoformat() if converted else str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth - 1) for k, v in list(value.items())[:200]}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth - 1) for v in list(value)[:200]]
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict(), depth - 1)
        except Exception:
            pass
    return clean_text(value)


def _write_preview(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TelegramCollector:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _window(self, source: Source) -> tuple[datetime, datetime, bool]:
        state = source.state
        first_run = not state or not state.bootstrap_completed
        now = datetime.now(UTC)
        frozen_end = None
        if state and state.post_cursor:
            raw_end = state.post_cursor.get("window_end")
            if raw_end:
                try:
                    frozen_end = datetime.fromisoformat(str(raw_end)).astimezone(UTC)
                except ValueError:
                    frozen_end = None
        base = (
            (state.monitor_from_at if state else None)
            or (state.checkpoint_at if state else None)
            or source.created_at
            or now
        )
        if not first_run and state and state.checkpoint_at:
            base = state.checkpoint_at - timedelta(seconds=self.settings.collection_overlap_seconds)
        return base.astimezone(UTC), frozen_end or now, first_run

    async def collect(self, source: Source, client: TelegramClient) -> CollectionResult:
        state = source.state
        window_start, window_end, first_run = self._window(source)
        username = source.normalized_link.rstrip("/").split("/")[-1]
        try:
            entity = await client.get_entity(username)
        except Exception as exc:
            raise _classify(exc) from exc

        entity_type = _entity_type(entity)
        entity_id = str(getattr(entity, "id", ""))
        title = _title(entity)
        normalized_link = f"https://t.me/{getattr(entity, 'username', None) or username}"
        probe_messages: list[Any] = []
        probe_method = getattr(client, "get_messages", None)
        if probe_method is not None:
            try:
                probe_messages = list(
                    await probe_method(
                        entity,
                        limit=max(1, getattr(self.settings, "credential_health_probe_posts", 5)),
                    )
                )
            except TypeError:
                # Lightweight test doubles and old adapters may require an
                # offset argument. Production Telethon clients support this call.
                probe_messages = []
            except Exception as exc:
                raise _classify(exc) from exc
        probe_messages = [message for message in probe_messages if getattr(message, "id", None)]
        content_probe_ok = bool(probe_messages)
        known_probe_match = False
        if state and state.recent_post_keys:
            known_ids = {str(key).rsplit(":", 1)[-1] for key in state.recent_post_keys if ":msg:" in str(key)}
            known_probe_match = any(str(message.id) in known_ids for message in probe_messages)
        items: list[CollectedItem] = []
        needs_retry = False
        old_watermark = int(state.post_watermark or 0) if state and str(state.post_watermark).isdigit() else 0
        new_watermark = old_watermark
        post_cursor = dict(state.post_cursor or {}) if state else {}
        post_keys: list[str] = []
        observed_post_ids: set[int] = set()

        if entity_type not in {"user", "bot"}:
            try:
                if old_watermark:
                    messages: list[Any] = []
                    async for message in client.iter_messages(
                        entity,
                        min_id=old_watermark,
                        reverse=True,
                        limit=self.settings.tg_batch_messages + 20,
                    ):
                        messages.append(message)
                    if len(messages) > self.settings.tg_batch_messages:
                        messages = self._keep_album_boundary(messages, self.settings.tg_batch_messages)
                        needs_retry = True
                    filtered = [
                        message
                        for message in messages
                        if (published := _utc(getattr(message, "date", None)))
                        and window_start < published <= window_end
                    ]
                    bounded_messages = [
                        message
                        for message in messages
                        if (published := _utc(getattr(message, "date", None))) and published <= window_end
                    ]
                    observed_post_ids.update(
                        int(getattr(message, "id", 0) or 0) for message in bounded_messages
                    )
                    observed_post_ids.discard(0)
                    if bounded_messages:
                        new_watermark = max(
                            new_watermark,
                            max(int(getattr(message, "id", 0) or 0) for message in bounded_messages),
                        )
                    post_cursor = {}
                else:
                    # First monitoring pass: walk backwards only until monitor_from_at.
                    # A cursor allows safe continuation without importing older history.
                    offset_id = int(post_cursor.get("offset_id") or 0)
                    candidate_watermark = int(post_cursor.get("candidate_watermark") or 0)
                    page = list(
                        await client.get_messages(
                            entity,
                            limit=self.settings.tg_batch_messages,
                            offset_id=offset_id,
                        )
                    )
                    page = [message for message in page if message and getattr(message, "id", None)]
                    bounded_page = [
                        message
                        for message in page
                        if (published := _utc(getattr(message, "date", None))) and published <= window_end
                    ]
                    observed_post_ids.update(int(getattr(message, "id", 0) or 0) for message in bounded_page)
                    observed_post_ids.discard(0)
                    if bounded_page:
                        candidate_watermark = max(
                            candidate_watermark,
                            max(int(getattr(message, "id", 0) or 0) for message in bounded_page),
                        )
                    barrier_reached = len(page) < self.settings.tg_batch_messages
                    filtered = []
                    for message in page:
                        published = _utc(getattr(message, "date", None))
                        if published and published <= window_start:
                            barrier_reached = True
                            continue
                        if published and published <= window_end:
                            filtered.append(message)
                    if barrier_reached or not page:
                        new_watermark = max(new_watermark, candidate_watermark)
                        post_cursor = {}
                    else:
                        oldest_id = min(int(getattr(message, "id", 0) or 0) for message in page)
                        post_cursor = {
                            "offset_id": oldest_id,
                            "candidate_watermark": candidate_watermark,
                            "window_end": window_end.isoformat(),
                        }
                        needs_retry = True
                    filtered.sort(key=lambda message: int(getattr(message, "id", 0) or 0))

                grouped = self._group_messages(filtered)
                for group in grouped.values():
                    item = await self._build_post(client, entity, username, group)
                    if item:
                        items.append(item)
                        post_keys.append(item.item_key)
            except Exception as exc:
                raise _classify(exc) from exc

        story_keys: list[str] = []
        known_story_keys = set(state.recent_story_keys or []) if state else set()
        old_story_watermark = (
            int(state.story_watermark or 0) if state and str(state.story_watermark).isdigit() else 0
        )
        max_story_id = old_story_watermark
        story_access_attempts = int((state.story_cursor or {}).get("access_attempts", 0)) if state else 0
        story_needs_retry = False
        story_cursor: dict[str, Any] = {}
        stories: list[Any] = []
        observed_story_ids: set[int] = set()
        try:
            stories = await self._collect_active_stories(client, entity)
            observed_story_ids.update(int(getattr(story, "id", 0) or 0) for story in stories)
            observed_story_ids.discard(0)
            for story in stories:
                story_id = int(getattr(story, "id", 0) or 0)
                max_story_id = max(max_story_id, story_id)
                story_key = f"tg:story:{entity_id}:{story_id}"
                if story_key in known_story_keys:
                    continue
                published = _utc(getattr(story, "date", None))
                if not published or not (window_start < published <= window_end):
                    continue
                item = await self._build_story(client, entity, username, story)
                if item:
                    items.append(item)
                    story_keys.append(item.item_key)
        except Exception as exc:
            # Stories failure must not discard successfully collected posts.
            story_error = _classify(exc)
            if isinstance(story_error, (CredentialDeadError, RateLimitedError, RetryableCollectorError)):
                raise story_error from exc
            if isinstance(story_error, AccessDeniedError):
                story_access_attempts += 1
                story_needs_retry = story_access_attempts < self.settings.max_credential_tries_per_source
                story_cursor = {"access_attempts": story_access_attempts, "last_status": "access_denied"}

        return CollectionResult(
            title=title,
            external_id=entity_id,
            normalized_link=normalized_link,
            items=items,
            post_watermark=str(new_watermark),
            story_watermark=str(max_story_id),
            post_cursor=post_cursor,
            story_cursor=story_cursor,
            window_start=window_start,
            window_end=window_end,
            needs_immediate_retry=needs_retry or story_needs_retry,
            diagnostics={
                "entity_type": entity_type,
                "first_run": first_run,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "post_keys": post_keys[-20:],
                "story_keys": story_keys[-20:],
                "batch_continuation": needs_retry,
                "story_needs_retry": story_needs_retry,
                "credential_content_probe_ok": content_probe_ok,
                "credential_known_post_match": known_probe_match,
                "credential_probe_ids": [int(message.id) for message in probe_messages[:10]],
                "observed_post_ids": sorted(observed_post_ids),
                "observed_story_ids": sorted(observed_story_ids),
                "api_request_count": 2 + int(needs_retry) + int(bool(stories)),
                "remote_latest_post_id": max(
                    [int(message.id) for message in probe_messages] or [new_watermark]
                ),
                "remote_latest_story_id": max(
                    [int(getattr(story, "id", 0) or 0) for story in stories] or [max_story_id]
                ),
            },
        )

    @staticmethod
    def _keep_album_boundary(messages: list[Any], limit: int) -> list[Any]:
        selected = messages[:limit]
        boundary_group = getattr(selected[-1], "grouped_id", None) if selected else None
        if boundary_group:
            for extra in messages[limit:]:
                if getattr(extra, "grouped_id", None) == boundary_group:
                    selected.append(extra)
                else:
                    break
        return selected

    @staticmethod
    def _group_messages(messages: list[Any]) -> OrderedDict[str, list[Any]]:
        groups: OrderedDict[str, list[Any]] = OrderedDict()
        for message in messages:
            if message is None or not getattr(message, "id", None):
                continue
            grouped_id = getattr(message, "grouped_id", None)
            key = f"album:{grouped_id}" if grouped_id else f"msg:{message.id}"
            groups.setdefault(key, []).append(message)
        return groups

    @staticmethod
    def _content_counts(messages: list[Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for message in messages:
            media = getattr(message, "media", None)
            if getattr(message, "photo", None) is not None:
                kind = "photo"
            elif getattr(message, "video", None) is not None:
                kind = "video"
            elif getattr(message, "document", None) is not None:
                mime = str(getattr(message.document, "mime_type", "") or "").lower()
                if mime.startswith("video/"):
                    kind = "video"
                elif mime.startswith("audio/"):
                    kind = "audio"
                elif mime.startswith("image/"):
                    kind = "photo"
                else:
                    kind = "document"
            elif getattr(media, "webpage", None) is not None:
                kind = "link"
            else:
                continue
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    async def _build_post(
        self,
        client: TelegramClient,
        entity: Any,
        username: str,
        messages: list[Any],
    ) -> CollectedItem | None:
        if not messages:
            return None
        ordered = sorted(messages, key=lambda message: int(getattr(message, "id", 0) or 0))
        representative = next(
            (
                message
                for message in ordered
                if clean_text(getattr(message, "message", "") or getattr(message, "raw_text", ""))
            ),
            ordered[0],
        )
        if not any(
            clean_text(getattr(message, "message", "") or getattr(message, "raw_text", ""))
            or getattr(message, "media", None)
            for message in ordered
        ):
            return None
        chat_id = str(getattr(entity, "id", ""))
        grouped_id = getattr(representative, "grouped_id", None)
        if grouped_id:
            key = f"tg:post:{chat_id}:album:{grouped_id}"
            external_id = f"album:{grouped_id}"
        else:
            key = f"tg:post:{chat_id}:msg:{representative.id}"
            external_id = str(representative.id)
        media = await self._download_message_previews(client, key, ordered)
        return CollectedItem(
            platform=Platform.TELEGRAM,
            item_type=ItemType.POST,
            item_key=key,
            external_id=external_id,
            original_url=f"https://t.me/{username}/{representative.id}" if username else "",
            text=clean_text(
                getattr(representative, "message", "") or getattr(representative, "raw_text", "")
            ),
            published_at=_utc(getattr(representative, "date", None)),
            media=media,
            raw={
                "representative": _jsonable(representative),
                "message_ids": [int(getattr(message, "id", 0) or 0) for message in ordered],
                "grouped_id": str(grouped_id or ""),
                "monitor_content_counts": self._content_counts(ordered),
                "monitor_forward": {
                    "is_forward": bool(getattr(representative, "fwd_from", None)),
                    "from_name": clean_text(
                        getattr(getattr(representative, "fwd_from", None), "from_name", "")
                    ),
                },
            },
        )

    @staticmethod
    def _thumbnail_sizes(media: Any) -> list[Any]:
        inner = getattr(media, "photo", None) or getattr(media, "document", None) or media
        return list(getattr(inner, "sizes", None) or getattr(inner, "thumbs", None) or [])

    def _thumb_candidates(self, media: Any) -> list[Any]:
        """Return downloadable image thumbnails from largest to smallest.

        Telegram places tiny ``PhotoStrippedSize`` placeholders in the same list as
        normal thumbnails. They are loading placeholders rather than usable final
        previews, so they are excluded completely.
        """
        ranked: list[tuple[int, int, int, Any]] = []
        for index, size in enumerate(self._thumbnail_sizes(media)):
            name = type(size).__name__
            if name in {"PhotoStrippedSize", "PhotoPathSize", "PhotoSizeEmpty"}:
                continue
            width = int(getattr(size, "w", 0) or 0)
            height = int(getattr(size, "h", 0) or 0)
            area = width * height
            file_size = int(getattr(size, "size", 0) or 0)
            ranked.append((area, file_size, index, size))
        ranked.sort(reverse=True, key=lambda row: (row[0], row[1], row[2]))
        return [size for _area, _file_size, _index, size in ranked]

    async def _prepare_preview(self, data: bytes) -> PreparedPreview | None:
        max_download_bytes = getattr(
            self.settings,
            "media_max_download_bytes",
            self.settings.media_max_preview_bytes,
        )
        if len(data) > max_download_bytes:
            return None
        return await asyncio.to_thread(
            prepare_preview,
            data,
            max_edge=self.settings.media_max_image_edge,
            max_bytes=self.settings.media_max_preview_bytes,
            min_edge=getattr(self.settings, "media_min_preview_edge", 320),
        )

    async def _download_preview_bytes(
        self,
        client: TelegramClient,
        media: Any,
        *,
        download_target: Any | None = None,
        allow_full_image: bool = False,
    ) -> PreparedPreview | None:
        inner = getattr(media, "photo", None) or getattr(media, "document", None) or media
        is_photo = "Photo" in type(inner).__name__
        target = download_target or media

        # Newer Telegram video media may expose a dedicated still cover. Prefer it
        # over the generic document thumbnail without downloading the video itself.
        video_cover = getattr(media, "video_cover", None)
        if video_cover is not None and video_cover is not media:
            cover = await self._download_preview_bytes(client, video_cover)
            if cover:
                return cover

        # Images sent as documents have only a low-resolution document thumbnail.
        # Download the full payload only when it is explicitly an image and fits the
        # strict download ceiling. Full videos/audio/documents are never requested.
        declared_size = int(getattr(inner, "size", 0) or 0)
        if allow_full_image and 0 < declared_size <= self.settings.media_max_download_bytes:
            try:
                raw = await client.download_media(target, file=bytes)
            except Exception:
                raw = None
            if isinstance(raw, (bytes, bytearray)) and raw:
                prepared = await self._prepare_preview(bytes(raw))
                if prepared:
                    return prepared

        # Telethon supports passing a PhotoSize object directly. Try the largest
        # normal image first, then progressively smaller ones. This avoids index
        # ambiguity and follows the library's documented thumbnail semantics.
        candidates = self._thumb_candidates(inner)
        for candidate in candidates:
            declared_size = int(getattr(candidate, "size", 0) or 0)
            if declared_size and declared_size > self.settings.media_max_download_bytes:
                continue
            try:
                raw = await client.download_media(target, file=bytes, thumb=candidate)
            except Exception:
                continue
            if not isinstance(raw, (bytes, bytearray)) or not raw:
                continue
            prepared = await self._prepare_preview(bytes(raw))
            if prepared:
                return prepared

        # Some photos expose no explicit normal sizes through lightweight adapters.
        # ``thumb=-1`` asks Telethon for the largest available thumbnail.
        if is_photo and not candidates:
            try:
                raw = await client.download_media(target, file=bytes, thumb=-1)
            except Exception:
                raw = None
            if isinstance(raw, (bytes, bytearray)) and raw:
                prepared = await self._prepare_preview(bytes(raw))
                if prepared:
                    return prepared

        # ``PhotoStrippedSize`` is only a tiny blurred placeholder used while a
        # real image loads. Sending it as the final preview produces visibly poor
        # notifications, so an item without a normal thumbnail is sent text-only.
        return None

    def _message_media(self, message: Any) -> tuple[Any | None, Any | None, str, bool]:
        media = getattr(message, "media", None)
        if getattr(message, "photo", None) is not None:
            return message.photo, message, "photo", False
        if getattr(message, "video", None) is not None:
            video_cover = getattr(media, "video_cover", None)
            if video_cover is not None:
                return video_cover, video_cover, "video_preview", False
            document = getattr(message, "document", None) or message.video
            return document, message, "video_preview", False
        if getattr(message, "document", None) is not None:
            document = message.document
            mime_type = str(getattr(document, "mime_type", "") or "").lower()
            if mime_type.startswith("image/"):
                return document, message, "photo", True
            return document, message, "document_preview", False
        webpage = getattr(media, "webpage", None)
        if webpage is not None:
            if getattr(webpage, "photo", None) is not None:
                return webpage.photo, message, "link_preview", False
            if getattr(webpage, "document", None) is not None:
                document = webpage.document
                mime_type = str(getattr(document, "mime_type", "") or "").lower()
                return document, message, "link_preview", mime_type.startswith("image/")
        return None, None, "", False

    @staticmethod
    def _message_duration(message: Any) -> int | None:
        document = getattr(message, "document", None) or getattr(message, "video", None)
        for attribute in getattr(document, "attributes", []) or []:
            duration = getattr(attribute, "duration", None)
            if duration:
                return int(duration)
        return None

    async def _download_message_previews(
        self,
        client: TelegramClient,
        item_key: str,
        messages: list[Any],
    ) -> list[MediaPayload]:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", item_key)
        target_dir = self.settings.media_root / "telegram" / safe_key
        result: list[MediaPayload] = []
        for message in messages:
            if len(result) >= self.settings.media_max_previews_per_item:
                break
            media_obj, download_target, media_type, allow_full_image = self._message_media(message)
            if media_obj is None:
                continue
            preview = await self._download_preview_bytes(
                client,
                media_obj,
                download_target=download_target,
                allow_full_image=allow_full_image,
            )
            if not preview:
                continue
            if media_type == "video_preview" and getattr(self.settings, "video_preview_overlay", True):
                duration = self._message_duration(message)
                preview = decorate_video_preview(
                    preview,
                    duration=duration,
                    index=sum(1 for row in result if row.media_type == "video_preview") + 1,
                    total=sum(1 for row in messages if self._message_media(row)[2] == "video_preview"),
                )
            path = target_dir / f"{len(result):02d}.jpg"
            await asyncio.to_thread(_write_preview, path, preview.data)
            result.append(
                MediaPayload(
                    media_type=media_type,
                    local_path=str(path),
                    mime_type=preview.mime_type,
                    width=preview.width,
                    height=preview.height,
                    metadata={
                        "message_id": int(getattr(message, "id", 0) or 0),
                        "preview_only": not allow_full_image,
                        "source": "full_image_document" if allow_full_image else "telegram_thumbnail",
                    },
                )
            )
        return result

    async def _collect_active_stories(self, client: TelegramClient, entity: Any) -> list[Any]:
        from telethon.tl import functions

        if not hasattr(functions, "stories"):
            return []
        stories_api = functions.stories
        input_peer = await client.get_input_entity(entity)
        active_response = await client(stories_api.GetPeerStoriesRequest(peer=input_peer))
        active = self._story_items(active_response)
        skipped_ids = [
            int(getattr(story, "id", 0) or 0)
            for story in active
            if type(story).__name__ == "StoryItemSkipped"
        ]
        if skipped_ids and hasattr(stories_api, "GetStoriesByIDRequest"):
            try:
                full_response = await client(
                    stories_api.GetStoriesByIDRequest(peer=input_peer, id=skipped_ids)
                )
                full = {
                    int(getattr(story, "id", 0) or 0): story for story in self._story_items(full_response)
                }
                active = [full.get(int(getattr(story, "id", 0) or 0), story) for story in active]
            except Exception:
                pass
        unique: dict[int, Any] = {}
        for story in active:
            story_id = int(getattr(story, "id", 0) or 0)
            if story_id and self._is_full_story(story):
                unique[story_id] = story
        return list(unique.values())

    @staticmethod
    def _story_items(response: Any) -> list[Any]:
        found: list[Any] = []

        def walk(value: Any, depth: int = 0) -> None:
            if depth > 8 or value is None:
                return
            name = type(value).__name__
            if name.startswith("StoryItem"):
                found.append(value)
                return
            if isinstance(value, (list, tuple)):
                for child in value:
                    walk(child, depth + 1)
                return
            if hasattr(value, "__dict__"):
                for child in value.__dict__.values():
                    walk(child, depth + 1)

        walk(response)
        return found

    @staticmethod
    def _is_full_story(story: Any) -> bool:
        return type(story).__name__ == "StoryItem" or hasattr(story, "media") or hasattr(story, "caption")

    async def _build_story(
        self,
        client: TelegramClient,
        entity: Any,
        username: str,
        story: Any,
    ) -> CollectedItem | None:
        story_id = int(getattr(story, "id", 0) or 0)
        if not story_id:
            return None
        entity_id = str(getattr(entity, "id", ""))
        key = f"tg:story:{entity_id}:{story_id}"
        media: list[MediaPayload] = []
        media_obj = getattr(story, "media", None)
        if media_obj is not None:
            preview = await self._download_preview_bytes(client, media_obj)
            if preview:
                safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
                target_dir = self.settings.media_root / "telegram" / safe_key
                path = target_dir / "story.jpg"
                await asyncio.to_thread(_write_preview, path, preview.data)
                media_type = "photo" if "Photo" in type(media_obj).__name__ else "video_preview"
                media.append(
                    MediaPayload(
                        media_type=media_type,
                        local_path=str(path),
                        mime_type=preview.mime_type,
                        width=preview.width,
                        height=preview.height,
                        metadata={"preview_only": True, "source": "telegram_thumbnail"},
                    )
                )
        return CollectedItem(
            platform=Platform.TELEGRAM,
            item_type=ItemType.STORY,
            item_key=key,
            external_id=str(story_id),
            original_url=f"https://t.me/{username}/s/{story_id}" if username else "",
            text=clean_text(getattr(story, "caption", "")),
            published_at=_utc(getattr(story, "date", None)),
            is_pinned=False,
            media=media,
            raw={
                "story": _jsonable(story),
                "source_kind": "active",
                "monitor_content_counts": {"video" if "Video" in type(media_obj).__name__ else "photo": 1}
                if media_obj is not None
                else {},
            },
        )

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from datetime import datetime, timezone
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
from app.utils.text import clean_text


BAD_SESSION_ERRORS = {
    "AuthKeyUnregisteredError", "AuthKeyInvalidError", "AuthKeyDuplicatedError",
    "SessionRevokedError", "SessionExpiredError", "SessionPasswordNeededError",
    "UserDeactivatedBanError", "UserDeactivatedError", "PhoneNumberBannedError",
}
NO_ACCESS_ERRORS = {
    "ChannelPrivateError", "ChatAdminRequiredError", "UserNotParticipantError",
    "InviteHashExpiredError", "InviteHashInvalidError", "ChatWriteForbiddenError",
}
NOT_FOUND_ERRORS = {"UsernameInvalidError", "UsernameNotOccupiedError", "UsernameNotModifiedError"}
RETRY_ERRORS = {
    "TimeoutError", "ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
    "ConnectionRefusedError", "OSError", "IncompleteReadError", "ServerError", "TimedOutError",
    "RpcCallFailError", "PhoneMigrateError", "NetworkMigrateError", "FileMigrateError",
}


def _exc_name(exc: BaseException) -> str:
    return type(exc).__name__


def _classify(exc: BaseException) -> Exception:
    name = _exc_name(exc)
    if name in BAD_SESSION_ERRORS:
        return CredentialDeadError(str(exc))
    if name in NO_ACCESS_ERRORS:
        return AccessDeniedError(str(exc))
    if name in NOT_FOUND_ERRORS:
        return NotFoundError(str(exc))
    if name == "FloodWaitError":
        return RateLimitedError(str(exc), retry_after=int(getattr(exc, "seconds", 60) or 60))
    if name in RETRY_ERRORS or isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return NetworkCollectorError(str(exc))
    return RetryableCollectorError(f"{name}: {exc}")


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
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _jsonable(value: Any, depth: int = 5) -> Any:
    if depth <= 0:
        return type(value).__name__
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()[:512]
    if isinstance(value, datetime):
        return _utc(value).isoformat() if _utc(value) else str(value)
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


class TelegramCollector:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def collect(self, source: Source, client: TelegramClient) -> CollectionResult:
        state = source.state
        bootstrap = not state or not state.bootstrap_completed
        username = source.normalized_link.rstrip("/").split("/")[-1]
        try:
            entity = await client.get_entity(username)
        except Exception as exc:
            raise _classify(exc) from exc

        entity_type = _entity_type(entity)
        entity_id = str(getattr(entity, "id", ""))
        title = _title(entity)
        normalized_link = f"https://t.me/{getattr(entity, 'username', None) or username}"
        items: list[CollectedItem] = []
        needs_retry = False
        old_watermark = int(state.post_watermark or 0) if state and str(state.post_watermark).isdigit() else 0
        new_watermark = old_watermark
        post_keys: list[str] = []

        if entity_type not in {"user", "bot"}:
            try:
                if bootstrap or old_watermark == 0:
                    messages = list(await client.get_messages(entity, limit=30))
                    messages.reverse()
                else:
                    messages = []
                    async for message in client.iter_messages(
                        entity,
                        min_id=old_watermark,
                        reverse=True,
                        limit=self.settings.tg_batch_messages + 20,
                    ):
                        messages.append(message)
                    if len(messages) > self.settings.tg_batch_messages:
                        selected = messages[: self.settings.tg_batch_messages]
                        boundary_group = getattr(selected[-1], "grouped_id", None) if selected else None
                        if boundary_group:
                            for extra in messages[self.settings.tg_batch_messages :]:
                                if getattr(extra, "grouped_id", None) == boundary_group:
                                    selected.append(extra)
                                else:
                                    break
                        messages = selected
                        needs_retry = True
                grouped = self._group_messages(messages)
                for group in grouped.values():
                    item = await self._build_post(client, entity, username, group)
                    if item:
                        items.append(item)
                        post_keys.append(item.item_key)
                        new_watermark = max(new_watermark, max(int(getattr(m, "id", 0) or 0) for m in group))
            except Exception as exc:
                raise _classify(exc) from exc

        story_keys: list[str] = []
        max_story_id = int(state.story_watermark or 0) if state and str(state.story_watermark).isdigit() else 0
        story_access_attempts = int((state.story_cursor or {}).get("access_attempts", 0)) if state else 0
        story_needs_retry = False
        story_cursor: dict[str, Any] = {}
        try:
            stories = await self._collect_stories(client, entity)
            for story, source_kind in stories:
                item = await self._build_story(client, entity, username, story, source_kind)
                if item:
                    items.append(item)
                    story_keys.append(item.item_key)
                    max_story_id = max(max_story_id, int(getattr(story, "id", 0) or 0))
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
            post_cursor={},
            story_cursor=story_cursor,
            needs_immediate_retry=needs_retry or story_needs_retry,
            diagnostics={
                "entity_type": entity_type,
                "bootstrap": bootstrap,
                "post_keys": post_keys[-20:],
                "story_keys": story_keys[-20:],
                "batch_full": needs_retry,
                "story_needs_retry": story_needs_retry,
            },
        )

    @staticmethod
    def _group_messages(messages: list[Any]) -> "OrderedDict[str, list[Any]]":
        groups: "OrderedDict[str, list[Any]]" = OrderedDict()
        for message in messages:
            if message is None or not getattr(message, "id", None):
                continue
            grouped_id = getattr(message, "grouped_id", None)
            key = f"album:{grouped_id}" if grouped_id else f"msg:{message.id}"
            groups.setdefault(key, []).append(message)
        return groups

    async def _build_post(
        self,
        client: TelegramClient,
        entity: Any,
        username: str,
        messages: list[Any],
    ) -> CollectedItem | None:
        if not messages:
            return None
        ordered = sorted(messages, key=lambda m: int(getattr(m, "id", 0) or 0))
        representative = next(
            (m for m in ordered if clean_text(getattr(m, "message", "") or getattr(m, "raw_text", ""))),
            ordered[0],
        )
        if not any(
            clean_text(getattr(m, "message", "") or getattr(m, "raw_text", ""))
            or getattr(m, "media", None)
            for m in ordered
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
        media = await self._download_message_media(client, key, ordered)
        return CollectedItem(
            platform=Platform.TELEGRAM,
            item_type=ItemType.POST,
            item_key=key,
            external_id=external_id,
            original_url=f"https://t.me/{username}/{representative.id}" if username else "",
            text=clean_text(getattr(representative, "message", "") or getattr(representative, "raw_text", "")),
            published_at=_utc(getattr(representative, "date", None)),
            media=media,
            raw={
                "representative": _jsonable(representative),
                "message_ids": [int(getattr(m, "id", 0) or 0) for m in ordered],
                "grouped_id": str(grouped_id or ""),
            },
        )

    async def _download_message_media(
        self, client: TelegramClient, item_key: str, messages: list[Any]
    ) -> list[MediaPayload]:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", item_key)
        target_dir = self.settings.media_root / "telegram" / safe_key
        target_dir.mkdir(parents=True, exist_ok=True)
        result: list[MediaPayload] = []
        for index, message in enumerate(messages):
            if not getattr(message, "media", None):
                continue
            try:
                path = await client.download_media(message, file=str(target_dir / f"{index:02d}"))
            except Exception:
                path = None
            media_type = "document"
            if getattr(message, "photo", None):
                media_type = "photo"
            elif getattr(message, "video", None):
                media_type = "video"
            elif getattr(message, "voice", None):
                media_type = "audio"
            result.append(
                MediaPayload(
                    media_type=media_type,
                    local_path=str(path or ""),
                    metadata={"message_id": int(getattr(message, "id", 0) or 0)},
                )
            )
        return result

    async def _collect_stories(self, client: TelegramClient, entity: Any) -> list[tuple[Any, str]]:
        from telethon.tl import functions

        if not hasattr(functions, "stories"):
            return []
        stories_api = functions.stories
        input_peer = await client.get_input_entity(entity)
        result: list[tuple[Any, str]] = []

        active_response = await client(stories_api.GetPeerStoriesRequest(peer=input_peer))
        active = self._story_items(active_response)
        skipped_ids = [int(getattr(x, "id", 0) or 0) for x in active if type(x).__name__ == "StoryItemSkipped"]
        if skipped_ids and hasattr(stories_api, "GetStoriesByIDRequest"):
            try:
                full_response = await client(stories_api.GetStoriesByIDRequest(peer=input_peer, id=skipped_ids))
                full = {int(getattr(x, "id", 0) or 0): x for x in self._story_items(full_response)}
                active = [full.get(int(getattr(x, "id", 0) or 0), x) for x in active]
            except Exception:
                pass
        result.extend((story, "active") for story in active)

        if hasattr(stories_api, "GetPinnedStoriesRequest"):
            offset_id = 0
            scanned = 0
            while scanned < 1000:
                response = await client(
                    stories_api.GetPinnedStoriesRequest(peer=input_peer, offset_id=offset_id, limit=100)
                )
                page = self._story_items(response)
                if not page:
                    break
                result.extend((story, "pinned") for story in page)
                scanned += len(page)
                last_id = int(getattr(page[-1], "id", 0) or 0)
                if not last_id or last_id == offset_id or len(page) < 100:
                    break
                offset_id = last_id

        unique: dict[int, tuple[Any, str]] = {}
        for story, source_kind in result:
            story_id = int(getattr(story, "id", 0) or 0)
            if story_id and self._is_full_story(story):
                unique[story_id] = (story, source_kind)
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
        source_kind: str,
    ) -> CollectedItem | None:
        story_id = int(getattr(story, "id", 0) or 0)
        if not story_id:
            return None
        entity_id = str(getattr(entity, "id", ""))
        key = f"tg:story:{entity_id}:{story_id}"
        media: list[MediaPayload] = []
        media_obj = getattr(story, "media", None)
        if media_obj is not None:
            safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
            target_dir = self.settings.media_root / "telegram" / safe_key
            target_dir.mkdir(parents=True, exist_ok=True)
            try:
                path = await client.download_media(media_obj, file=str(target_dir / "story"))
            except Exception:
                path = None
            media_type = "photo" if "Photo" in type(media_obj).__name__ else "video" if "Document" in type(media_obj).__name__ else "document"
            media.append(MediaPayload(media_type=media_type, local_path=str(path or "")))
        return CollectedItem(
            platform=Platform.TELEGRAM,
            item_type=ItemType.STORY,
            item_key=key,
            external_id=str(story_id),
            original_url=f"https://t.me/{username}/s/{story_id}" if username else "",
            text=clean_text(getattr(story, "caption", "")),
            published_at=_utc(getattr(story, "date", None)),
            is_pinned=source_kind == "pinned",
            media=media,
            raw={"story": _jsonable(story), "source_kind": source_kind},
        )

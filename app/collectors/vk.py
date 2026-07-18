from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from app.collectors.errors import (
    AccessDeniedError,
    CredentialDeadError,
    FeatureUnavailableError,
    NetworkCollectorError,
    NotFoundError,
    ProxyUnavailableError,
    RateLimitedError,
    RetryableCollectorError,
)
from app.collectors.types import CollectedItem, CollectionResult, MediaPayload
from app.config import Settings
from app.db.enums import ItemType, Platform
from app.db.models import Source
from app.services.network import proxy_session
from app.utils.text import clean_text

TOKEN_DEAD_CODES = {5, 14, 17, 27, 28}
RETRY_CODES = {6, 9, 10, 29}
ACCESS_CODES = {15, 200, 201, 203}
METHOD_UNAVAILABLE_CODES = {3, 7}
NOT_FOUND_CODES = {18, 30, 100, 113}


class VkApiError(RuntimeError):
    def __init__(self, error: dict[str, Any]):
        self.error = error
        self.code = int(error.get("error_code") or 0)
        self.message = str(error.get("error_msg") or error)
        super().__init__(f"VK {self.code}: {self.message}")


class VkCollector:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token_next: dict[str, float] = {}
        self._rate_lock = asyncio.Lock()

    async def _rate_limit(self, token: str) -> None:
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            next_allowed = self._token_next.get(token, 0.0)
            delay = max(0.0, next_allowed - now)
            self._token_next[token] = max(now, next_allowed) + self.settings.vk_per_token_min_interval_seconds
        if delay:
            await asyncio.sleep(delay)

    async def _call(
        self,
        session: aiohttp.ClientSession,
        request_proxy: str | None,
        token: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        await self._rate_limit(token)
        payload = {**params, "access_token": token, "v": self.settings.vk_api_version}
        try:
            async with session.post(
                f"{self.settings.vk_api_base}/{method}",
                data=payload,
                proxy=request_proxy,
            ) as response:
                text = await response.text()
                if response.status == 407:
                    raise ProxyUnavailableError("Proxy authentication failed")
                if response.status >= 500:
                    raise RetryableCollectorError(f"VK HTTP {response.status}")
                if response.status != 200:
                    raise RetryableCollectorError(f"VK HTTP {response.status}: {text[:300]}")
                data = json.loads(text)
        except ProxyUnavailableError:
            raise
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise NetworkCollectorError(str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise RetryableCollectorError(str(exc)) from exc
        if "error" in data:
            error = VkApiError(data["error"])
            if error.code in TOKEN_DEAD_CODES:
                raise CredentialDeadError(str(error)) from error
            if error.code in RETRY_CODES:
                raise RateLimitedError(str(error), retry_after=2) from error
            if error.code in METHOD_UNAVAILABLE_CODES:
                raise FeatureUnavailableError(str(error)) from error
            if error.code in ACCESS_CODES:
                raise AccessDeniedError(str(error)) from error
            if error.code in NOT_FOUND_CODES:
                raise NotFoundError(str(error)) from error
            raise RetryableCollectorError(str(error)) from error
        return data

    @staticmethod
    def _parse_owner_hint(source: Source) -> tuple[int | None, str]:
        stored_external = str(source.external_id or "").strip()
        if stored_external and (stored_external.isdigit() or (stored_external.startswith("-") and stored_external[1:].isdigit())):
            owner_id = int(stored_external)
            return owner_id, "user" if owner_id > 0 else "group"
        identifier = source.normalized_link.rstrip("/").split("/")[-1]
        if match := re.fullmatch(r"id(\d+)", identifier, re.I):
            return int(match.group(1)), "user"
        if match := re.fullmatch(r"(?:club|public|event)(\d+)", identifier, re.I):
            return -int(match.group(1)), "group"
        if re.fullmatch(r"-?\d+", identifier):
            owner_id = int(identifier)
            return owner_id, "user" if owner_id > 0 else "group"
        if match := re.fullmatch(r"wall(-?\d+)_\d+", identifier, re.I):
            owner_id = int(match.group(1))
            return owner_id, "user" if owner_id > 0 else "group"
        if match := re.fullmatch(r"story(-?\d+)_\d+", identifier, re.I):
            owner_id = int(match.group(1))
            return owner_id, "user" if owner_id > 0 else "group"
        return None, identifier

    async def _resolve_owner(
        self,
        session: aiohttp.ClientSession,
        request_proxy: str | None,
        token: str,
        source: Source,
    ) -> tuple[int, str, str, str]:
        owner_id, hint = self._parse_owner_hint(source)
        owner_type = hint if owner_id is not None else "unknown"
        screen_name = "" if owner_id is not None else hint
        if owner_id is not None and source.title:
            identifier = source.normalized_link.rstrip("/").split("/")[-1]
            screen_name = identifier if not identifier.lstrip("-").isdigit() else ""
            return owner_id, owner_type, source.title, screen_name

        if owner_id is None:
            data = await self._call(
                session, request_proxy, token, "utils.resolveScreenName", {"screen_name": screen_name}
            )
            obj = data.get("response") or {}
            object_id = obj.get("object_id")
            object_type = obj.get("type")
            if not object_id or object_type not in {"user", "group", "page"}:
                raise NotFoundError("VK screen name not found")
            owner_id = int(object_id) if object_type == "user" else -int(object_id)
            owner_type = object_type

        if owner_id > 0:
            data = await self._call(
                session,
                request_proxy,
                token,
                "users.get",
                {"user_ids": owner_id, "fields": "screen_name,domain,is_closed,can_access_closed"},
            )
            users = data.get("response") or []
            if not users:
                raise NotFoundError("VK user not found")
            user = users[0]
            if user.get("deactivated"):
                raise NotFoundError("VK user deleted or banned")
            if user.get("is_closed") and not user.get("can_access_closed"):
                raise AccessDeniedError("VK profile is private")
            title = clean_text(f"{user.get('first_name', '')} {user.get('last_name', '')}")
            screen_name = str(user.get("screen_name") or user.get("domain") or screen_name)
            return owner_id, "user", title, screen_name

        data = await self._call(
            session,
            request_proxy,
            token,
            "groups.getById",
            {"group_ids": abs(owner_id), "fields": "screen_name,is_closed,type"},
        )
        groups = data.get("response") or []
        if isinstance(groups, dict):
            groups = groups.get("groups") or []
        if not groups:
            raise NotFoundError("VK group not found")
        group = groups[0]
        if group.get("deactivated"):
            raise NotFoundError("VK group deleted or banned")
        title = clean_text(group.get("name"))
        screen_name = str(group.get("screen_name") or screen_name)
        return owner_id, str(group.get("type") or owner_type), title, screen_name

    def _best_photo(self, photo: dict[str, Any]) -> tuple[str, int | None, int | None]:
        sizes = [x for x in (photo.get("sizes") or []) if isinstance(x, dict) and x.get("url")]
        if sizes:
            within = [
                x for x in sizes
                if max(int(x.get("width") or 0), int(x.get("height") or 0)) <= self.settings.media_max_image_edge
            ]
            candidates = within or sizes
            best = max(
                candidates,
                key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0),
            )
            return str(best.get("url") or ""), best.get("width"), best.get("height")
        url = str(photo.get("photo_807") or photo.get("photo_604") or photo.get("url") or "")
        return url, None, None

    def _best_image(self, images: Any) -> dict[str, Any]:
        candidates = [x for x in (images or []) if isinstance(x, dict) and x.get("url")]
        if not candidates:
            return {}
        within = [
            x for x in candidates
            if max(int(x.get("width") or 0), int(x.get("height") or 0)) <= self.settings.media_max_image_edge
        ]
        return max(
            within or candidates,
            key=lambda x: int(x.get("width") or 0) * int(x.get("height") or 0),
        )

    def _media_from_attachments(self, attachments: Any) -> list[MediaPayload]:
        result: list[MediaPayload] = []
        if not isinstance(attachments, list):
            return result
        for attachment in attachments:
            if len(result) >= self.settings.media_max_previews_per_item:
                break
            if not isinstance(attachment, dict):
                continue
            kind = str(attachment.get("type") or "unknown")
            obj = attachment.get(kind) or {}
            if kind == "photo":
                url, width, height = self._best_photo(obj)
                if url:
                    result.append(MediaPayload("photo", preview_url=url, width=width, height=height))
            elif kind == "video":
                best = self._best_image(obj.get("image") or obj.get("first_frame") or [])
                owner_id = obj.get("owner_id")
                video_id = obj.get("id")
                video_url = (
                    f"https://vk.com/video{owner_id}_{video_id}"
                    if owner_id is not None and video_id is not None
                    else ""
                )
                if best.get("url"):
                    result.append(
                        MediaPayload(
                            "video_preview",
                            remote_url=video_url,
                            preview_url=str(best.get("url") or ""),
                            width=best.get("width"),
                            height=best.get("height"),
                            metadata={"preview_only": True},
                        )
                    )
            elif kind == "link":
                url, width, height = self._best_photo(obj.get("photo") or {})
                if url:
                    result.append(
                        MediaPayload(
                            "link_preview",
                            remote_url=str(obj.get("url") or ""),
                            preview_url=url,
                            width=width,
                            height=height,
                            metadata={"preview_only": True},
                        )
                    )
        return result

    async def _download_images(
        self,
        session: aiohttp.ClientSession,
        request_proxy: str | None,
        item_key: str,
        media: list[MediaPayload],
    ) -> None:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", item_key)
        target_dir = self.settings.media_root / "vk" / safe_key
        downloaded = 0
        for payload in media:
            if downloaded >= self.settings.media_max_previews_per_item:
                break
            url = payload.preview_url
            if not url or not url.startswith(("http://", "https://")):
                continue
            try:
                async with session.get(url, proxy=request_proxy) as response:
                    if response.status != 200:
                        continue
                    declared = int(response.headers.get("Content-Length") or 0)
                    if declared and declared > self.settings.media_max_preview_bytes:
                        continue
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        content.extend(chunk)
                        if len(content) > self.settings.media_max_preview_bytes:
                            content.clear()
                            break
                    if not content:
                        continue
                    target_dir.mkdir(parents=True, exist_ok=True)
                    suffix = Path(urlparse(url).path).suffix.lower()
                    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                        suffix = ".jpg"
                    path = target_dir / f"{downloaded:02d}{suffix}"
                    path.write_bytes(content)
                    payload.local_path = str(path)
                    payload.mime_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
                    downloaded += 1
            except Exception:
                continue

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

    async def collect(
        self,
        source: Source,
        *,
        token: str,
        proxy_url: str,
    ) -> CollectionResult:
        state = source.state
        window_start, window_end, first_run = self._window(source)
        post_cursor = dict(state.post_cursor or {}) if state else {}
        offset = max(0, int(post_cursor.get("offset") or 0))
        candidate_watermark = int(post_cursor.get("candidate_watermark") or 0)
        known_recent = set(state.recent_post_keys or []) if state else set()
        old_watermark = int(state.post_watermark or 0) if state and str(state.post_watermark).isdigit() else 0

        async with proxy_session(proxy_url, timeout_seconds=60) as (session, request_proxy):
            owner_id, owner_type, title, screen_name = await self._resolve_owner(
                session, request_proxy, token, source
            )
            normalized_link = f"https://vk.com/{screen_name}" if screen_name else source.normalized_link

            items: list[CollectedItem] = []
            selected_posts: dict[str, dict[str, Any]] = {}
            found_barrier = False
            pages = 0

            while pages < self.settings.vk_max_pages_per_run:
                data = await self._call(
                    session,
                    request_proxy,
                    token,
                    "wall.get",
                    {
                        "owner_id": owner_id,
                        "count": self.settings.vk_page_size,
                        "offset": offset,
                        "filter": "owner",
                    },
                )
                response = data.get("response") or {}
                page = response.get("items") or []
                if not page:
                    found_barrier = True
                    break
                pages += 1
                stop_page = False
                for post in page:
                    post_owner = int(post.get("owner_id") or owner_id)
                    post_id = int(post.get("id") or 0)
                    if not post_id:
                        continue
                    key = f"vk:post:{post_owner}:{post_id}"
                    published = (
                        datetime.fromtimestamp(int(post.get("date") or 0), tz=UTC)
                        if post.get("date")
                        else None
                    )
                    is_pinned = int(post.get("is_pinned") or 0) == 1
                    if published and published <= window_end:
                        candidate_watermark = max(candidate_watermark, post_id)

                    # An old pinned post is not a barrier and is never a new event.
                    if is_pinned:
                        if published and window_start < published <= window_end and key not in known_recent:
                            selected_posts[key] = post
                        continue

                    if old_watermark and (key in known_recent or post_id <= old_watermark):
                        found_barrier = True
                        stop_page = True
                        break
                    if published and published <= window_start:
                        found_barrier = True
                        stop_page = True
                        break
                    if published and published <= window_end:
                        selected_posts[key] = post

                if stop_page:
                    break
                if len(page) < self.settings.vk_page_size:
                    found_barrier = True
                    break
                offset += self.settings.vk_page_size

            max_post_id = old_watermark
            new_post_keys: list[str] = []
            for post in sorted(selected_posts.values(), key=lambda row: int(row.get("date") or 0)):
                post_owner = int(post.get("owner_id") or owner_id)
                post_id = int(post.get("id") or 0)
                key = f"vk:post:{post_owner}:{post_id}"
                max_post_id = max(max_post_id, post_id)
                new_post_keys.append(key)
                attachments = list(post.get("attachments") or [])
                copy_history = post.get("copy_history") or []
                repost_text = ""
                if isinstance(copy_history, list) and copy_history:
                    original = copy_history[0] or {}
                    attachments.extend(original.get("attachments") or [])
                    repost_text = clean_text(original.get("text"))
                media = self._media_from_attachments(attachments)
                published = datetime.fromtimestamp(int(post.get("date") or 0), tz=UTC)
                post_text = clean_text(post.get("text"))
                if repost_text:
                    post_text = f"{post_text}\n\n{repost_text}".strip()
                item = CollectedItem(
                    platform=Platform.VK,
                    item_type=ItemType.POST,
                    item_key=key,
                    external_id=f"{post_owner}_{post_id}",
                    original_url=f"https://vk.com/wall{post_owner}_{post_id}",
                    text=post_text,
                    published_at=published,
                    is_pinned=int(post.get("is_pinned") or 0) == 1,
                    media=media,
                    raw=post,
                )
                await self._download_images(session, request_proxy, key, media)
                items.append(item)

            if found_barrier:
                max_post_id = max(max_post_id, candidate_watermark)
                next_post_cursor: dict[str, Any] = {}
            else:
                next_post_cursor = {
                    "offset": offset,
                    "candidate_watermark": candidate_watermark,
                    "window_end": window_end.isoformat(),
                }

            story_access_attempts = int((state.story_cursor or {}).get("access_attempts", 0)) if state else 0
            story_needs_retry = False
            story_cursor: dict[str, Any] = {}
            try:
                try:
                    data = await self._call(
                        session,
                        request_proxy,
                        token,
                        "stories.get",
                        {"owner_id": owner_id, "extended": 1},
                    )
                except NotFoundError:
                    data = await self._call(
                        session,
                        request_proxy,
                        token,
                        "stories.get",
                        {"extended": 1},
                    )
                raw_stories = [
                    story
                    for story in self._extract_story_objects(data.get("response") or data)
                    if int(story.get("owner_id") or 0) == owner_id
                ]
            except AccessDeniedError:
                raw_stories = []
                story_access_attempts += 1
                story_needs_retry = story_access_attempts < self.settings.max_credential_tries_per_source
                story_cursor = {"access_attempts": story_access_attempts, "last_status": "access_denied"}
            except (NotFoundError, FeatureUnavailableError):
                raw_stories = []

            story_keys: list[str] = []
            known_story_keys = set(state.recent_story_keys or []) if state else set()
            max_story_id = int(state.story_watermark or 0) if state and str(state.story_watermark).isdigit() else 0
            for story in raw_stories:
                story_owner = int(story.get("owner_id") or owner_id)
                story_id = int(story.get("id") or 0)
                if not story_id:
                    continue
                max_story_id = max(max_story_id, story_id)
                key = f"vk:story:{story_owner}:{story_id}"
                if key in known_story_keys:
                    continue
                published = (
                    datetime.fromtimestamp(int(story.get("date") or 0), tz=UTC)
                    if story.get("date")
                    else None
                )
                if not published or not (window_start < published <= window_end):
                    continue
                story_keys.append(key)
                story_media: list[MediaPayload] = []
                if isinstance(story.get("photo"), dict):
                    url, width, height = self._best_photo(story["photo"])
                    if url:
                        story_media.append(MediaPayload("photo", preview_url=url, width=width, height=height))
                elif isinstance(story.get("video"), dict):
                    video = story["video"]
                    best = self._best_image(video.get("image") or video.get("first_frame") or [])
                    if best.get("url"):
                        story_media.append(
                            MediaPayload(
                                "video_preview",
                                preview_url=str(best.get("url") or ""),
                                metadata={"preview_only": True},
                            )
                        )
                item = CollectedItem(
                    platform=Platform.VK,
                    item_type=ItemType.STORY,
                    item_key=key,
                    external_id=f"{story_owner}_{story_id}",
                    original_url=f"https://vk.com/story{story_owner}_{story_id}",
                    text=clean_text(story.get("text") or story.get("caption")),
                    published_at=published,
                    media=story_media,
                    raw=story,
                )
                await self._download_images(session, request_proxy, key, story_media)
                items.append(item)

        needs_retry = (not found_barrier) or story_needs_retry
        return CollectionResult(
            title=title,
            external_id=str(owner_id),
            normalized_link=normalized_link,
            items=items,
            post_watermark=str(max_post_id),
            story_watermark=str(max_story_id),
            post_cursor=next_post_cursor,
            story_cursor=story_cursor,
            window_start=window_start,
            window_end=window_end,
            needs_immediate_retry=needs_retry,
            diagnostics={
                "owner_type": owner_type,
                "pages": pages,
                "found_barrier": found_barrier,
                "first_run": first_run,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "post_keys": new_post_keys[-20:],
                "story_keys": story_keys[-20:],
                "story_needs_retry": story_needs_retry,
            },
        )

    @staticmethod
    def _extract_story_objects(data: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if "id" in value and "owner_id" in value and (
                    "date" in value or "expires_at" in value or "photo" in value or "video" in value
                ):
                    found.append(value)
                    return
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(data)
        unique: dict[tuple[int, int], dict[str, Any]] = {}
        for story in found:
            try:
                unique[(int(story["owner_id"]), int(story["id"]))] = story
            except Exception:
                continue
        return list(unique.values())

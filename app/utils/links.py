from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from app.db.enums import Platform


@dataclass(slots=True)
class NormalizedSourceLink:
    platform: Platform
    input_link: str
    normalized_link: str
    identifier: str
    kind: str


VK_DOMAINS = {
    "vk.com", "www.vk.com", "m.vk.com",
    "vk.ru", "www.vk.ru", "m.vk.ru",
    "vkontakte.ru", "www.vkontakte.ru", "m.vkontakte.ru",
}
TG_DOMAINS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}


def detect_platform(raw: str) -> Platform:
    value = (raw or "").strip().lower()
    if any(domain in value for domain in ("vk.com", "vk.ru", "vkontakte.ru")):
        return Platform.VK
    if any(domain in value for domain in ("t.me", "telegram.me")) or value.startswith("@"):
        return Platform.TELEGRAM
    # A bare token is ambiguous; Telegram usernames are the least surprising fallback.
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
        return Platform.TELEGRAM
    raise ValueError("Не удалось определить платформу ссылки")


def normalize_source_link(raw: str) -> NormalizedSourceLink:
    platform = detect_platform(raw)
    if platform == Platform.VK:
        return normalize_vk_link(raw)
    if platform == Platform.TELEGRAM:
        return normalize_tg_link(raw)
    raise ValueError("Платформа пока не поддерживается")


def normalize_tg_link(raw: str) -> NormalizedSourceLink:
    original = (raw or "").strip()
    if not original:
        raise ValueError("Пустая ссылка")
    if original.startswith("@"):
        username = original[1:].strip().strip("/")
    elif re.fullmatch(r"[A-Za-z0-9_]{5,32}", original):
        username = original
    else:
        candidate = original
        if candidate.startswith(("t.me/", "telegram.me/")):
            candidate = "https://" + candidate
        parsed = urlparse(candidate)
        if parsed.netloc.lower() not in TG_DOMAINS:
            raise ValueError("Это не ссылка Telegram")
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if not parts:
            raise ValueError("В ссылке нет username")
        if parts[0].lower() == "s" and len(parts) >= 2:
            username = parts[1]
        else:
            username = parts[0]
        if username.lower() in {"c", "joinchat"} or username.startswith("+"):
            raise ValueError("Приватные invite-ссылки в v1 не поддерживаются")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("Некорректный Telegram username")
    normalized = f"https://t.me/{username}"
    return NormalizedSourceLink(Platform.TELEGRAM, original, normalized, username.lower(), "profile")


def _strip_vk_domain(raw: str) -> tuple[str, dict[str, list[str]]]:
    value = (raw or "").strip()
    if not value:
        return "", {}
    if re.match(r"^(?:www\.|m\.)?(?:vk\.com|vk\.ru|vkontakte\.ru)/", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.netloc.lower() in VK_DOMAINS:
        return unquote(parsed.path or "").strip("/"), parse_qs(parsed.query or "")
    return value.split("?", 1)[0].split("#", 1)[0].strip().strip("/"), {}


def normalize_vk_link(raw: str) -> NormalizedSourceLink:
    original = (raw or "").strip()
    token, query = _strip_vk_domain(original)
    if token.startswith("@"):
        token = token[1:]
    lowered = token.lower().strip("/")
    if not lowered:
        raise ValueError("Пустая VK-ссылка")

    wall = re.fullmatch(r"wall(-?\d+)_(\d+)", lowered)
    if wall:
        owner_id, post_id = wall.groups()
        identifier = f"wall{owner_id}_{post_id}"
        return NormalizedSourceLink(Platform.VK, original, f"https://vk.com/{identifier}", identifier, "wall_post")

    story = re.fullmatch(r"story(-?\d+)_(\d+)", lowered)
    if story:
        owner_id, story_id = story.groups()
        identifier = f"story{owner_id}_{story_id}"
        return NormalizedSourceLink(Platform.VK, original, f"https://vk.com/{identifier}", identifier, "story")

    for values in query.values():
        for value in values:
            value = unquote(value)
            match = re.search(r"wall(-?\d+)_(\d+)", value)
            if match:
                identifier = f"wall{match.group(1)}_{match.group(2)}"
                return NormalizedSourceLink(Platform.VK, original, f"https://vk.com/{identifier}", identifier, "wall_post")
            match = re.search(r"story(-?\d+)_(\d+)", value)
            if match:
                identifier = f"story{match.group(1)}_{match.group(2)}"
                return NormalizedSourceLink(Platform.VK, original, f"https://vk.com/{identifier}", identifier, "story")

    if re.match(r"^(video|photo|market|album|audio|doc|topic|app|im|feed|friends|groups|mail|write)", lowered):
        raise ValueError("Этот тип VK-ссылки не поддерживается")
    if re.fullmatch(r"id\d+|(?:club|public|event)\d+|-?\d+", lowered):
        identifier = lowered
    elif re.fullmatch(r"[A-Za-z0-9_.]{2,80}", token):
        identifier = token
    else:
        raise ValueError("Не удалось разобрать VK-ссылку")
    return NormalizedSourceLink(Platform.VK, original, f"https://vk.com/{identifier}", identifier.lower(), "profile")

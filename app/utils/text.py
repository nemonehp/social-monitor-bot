from __future__ import annotations

import html
import re
from collections.abc import Iterator

VK_MARKUP_RE = re.compile(
    r"\[(?P<target>https?://[^|\]]+|(?:id|club|public|event)-?\d+)\|(?P<label>[^\]]+)\]"
)


def clean_text(value: object, max_len: int | None = None) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x00", "")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r" {2,}", " ", text).strip()
    if max_len and len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _vk_target_url(target: str) -> str:
    if target.startswith(("http://", "https://")):
        return target
    normalized = target
    if target.startswith("club"):
        normalized = target
    elif target.startswith("public"):
        normalized = target
    elif target.startswith("event"):
        normalized = target
    elif target.startswith("id"):
        normalized = target
    return f"https://vk.com/{normalized}"


def vk_text_to_html(value: str) -> str:
    """Render VK's ``[target|label]`` syntax as safe Telegram HTML links."""
    result: list[str] = []
    cursor = 0
    for match in VK_MARKUP_RE.finditer(value):
        result.append(h(value[cursor : match.start()]))
        target = _vk_target_url(match.group("target"))
        label = h(match.group("label"))
        result.append(f'<a href="{html.escape(target, quote=True)}">{label}</a>')
        cursor = match.end()
    result.append(h(value[cursor:]))
    return "".join(result)


def split_text(value: str, limit: int) -> Iterator[str]:
    """Split source text without losing characters, preferring natural boundaries."""
    remaining = value.strip()
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        cut = max(1, limit)
        for separator in ("\n\n", "\n", ". ", " "):
            position = remaining.rfind(separator, 0, limit + 1)
            if position >= limit // 2:
                cut = position + len(separator)
                break
        yield remaining[:cut].rstrip()
        remaining = remaining[cut:].lstrip()

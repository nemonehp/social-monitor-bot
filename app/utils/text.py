from __future__ import annotations

import html
import re


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

from __future__ import annotations

from app.db.enums import Platform

_PLATFORM_BADGES = {
    Platform.TELEGRAM.value: "🔵 TG",
    Platform.VK.value: "🟢 VK",
    Platform.MAX.value: "⚪ MAX",
}


def platform_badge(platform: Platform | str) -> str:
    """Return one consistent, plain-Unicode platform label for bot UI and signals."""
    value = platform.value if isinstance(platform, Platform) else str(platform).lower()
    return _PLATFORM_BADGES.get(value, f"⚪ {value.upper()}")

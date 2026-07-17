from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse


SUPPORTED_SCHEMES = {"http", "https", "socks4", "socks5"}
SCHEME_ALIASES = {"socks": "socks5", "socks5h": "socks5"}


@dataclass(slots=True)
class ParsedProxy:
    url: str
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def display(self) -> str:
        auth = f"{self.username}:***@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()


def parse_proxy_line(raw: str, default_scheme: str = "http") -> ParsedProxy:
    value = (raw or "").strip()
    if not value or value.startswith("#"):
        raise ValueError("Пустая строка прокси")

    if "://" not in value:
        # host:port:user:pass
        parts = value.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            host, port, username, password = parts
            value = f"{default_scheme}://{quote(username)}:{quote(password)}@{host}:{port}"
        elif re.fullmatch(r"[^:@\s]+:[^@\s]+@[^:\s]+:\d+", value):
            value = f"{default_scheme}://{value}"
        elif len(parts) == 2 and parts[1].isdigit():
            value = f"{default_scheme}://{value}"
        else:
            raise ValueError("Неизвестный формат прокси")

    parsed = urlparse(value)
    scheme = SCHEME_ALIASES.get(parsed.scheme.lower(), parsed.scheme.lower())
    if scheme not in SUPPORTED_SCHEMES:
        raise ValueError(f"Неподдерживаемая схема: {scheme}")
    if not parsed.hostname or not parsed.port:
        raise ValueError("У прокси должны быть host и port")
    if not (1 <= parsed.port <= 65535):
        raise ValueError("Некорректный порт")

    username = parsed.username or ""
    password = parsed.password or ""
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    canonical = f"{scheme}://{auth}{parsed.hostname}:{parsed.port}"
    return ParsedProxy(canonical, scheme, parsed.hostname, parsed.port, username, password)

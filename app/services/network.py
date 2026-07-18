from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiohttp
from aiohttp_socks import ProxyConnector


@dataclass(slots=True)
class IpInfo:
    ip: str
    country_code: str
    latency_ms: int


@asynccontextmanager
async def proxy_session(proxy_url: str, timeout_seconds: int = 30) -> AsyncIterator[tuple[aiohttp.ClientSession, str | None]]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(15, timeout_seconds))
    if proxy_url.lower().startswith(("socks4://", "socks5://")):
        connector = ProxyConnector.from_url(proxy_url)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            yield session, None
    else:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            yield session, proxy_url


async def check_direct_ip(ip_check_url: str) -> IpInfo:
    started = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=20, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(ip_check_url) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
    latency_ms = int((time.monotonic() - started) * 1000)
    success = data.get("success", True)
    if success is False:
        raise RuntimeError(str(data.get("message") or "IP check failed"))
    country_code = str(data.get("country_code") or data.get("countryCode") or data.get("cc") or "").upper()
    ip = str(data.get("ip") or data.get("query") or "")
    if not country_code or not ip:
        raise RuntimeError("IP check endpoint returned incomplete data")
    return IpInfo(ip=ip, country_code=country_code, latency_ms=latency_ms)


async def check_proxy(proxy_url: str, ip_check_url: str) -> IpInfo:
    started = time.monotonic()
    async with proxy_session(proxy_url) as (session, request_proxy):
        async with session.get(ip_check_url, proxy=request_proxy, ssl=True) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
    latency_ms = int((time.monotonic() - started) * 1000)
    success = data.get("success", True)
    if success is False:
        raise RuntimeError(str(data.get("message") or "IP check failed"))
    country_code = str(data.get("country_code") or data.get("countryCode") or data.get("cc") or "").upper()
    ip = str(data.get("ip") or data.get("query") or "")
    if not country_code or not ip:
        raise RuntimeError("IP check endpoint returned incomplete data")
    return IpInfo(ip=ip, country_code=country_code, latency_ms=latency_ms)


async def check_vk_access(proxy_url: str) -> None:
    async with proxy_session(proxy_url) as (session, request_proxy):
        async with session.get("https://api.vk.com/method/utils.getServerTime?v=5.131", proxy=request_proxy) as response:
            if response.status >= 500:
                raise RuntimeError(f"VK endpoint returned HTTP {response.status}")
            data = await response.json(content_type=None)
            if not isinstance(data, dict):
                raise RuntimeError("VK endpoint returned invalid response")

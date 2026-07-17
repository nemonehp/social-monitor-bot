from __future__ import annotations

from typing import Protocol

from app.collectors.types import CollectionResult
from app.db.models import Source


class Collector(Protocol):
    async def collect(self, source: Source, **kwargs) -> CollectionResult: ...

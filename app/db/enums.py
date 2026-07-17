from enum import StrEnum


class Platform(StrEnum):
    VK = "vk"
    TELEGRAM = "telegram"
    MAX = "max"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    DELETED = "deleted"


class ItemType(StrEnum):
    POST = "post"
    STORY = "story"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    RETRY = "retry"
    FAILED = "failed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"


class CredentialPlatform(StrEnum):
    VK = "vk"
    TELEGRAM = "telegram"


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    DEAD = "dead"
    DISABLED = "disabled"


class ProxyStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    QUARANTINE = "quarantine"
    REMOVED = "removed"

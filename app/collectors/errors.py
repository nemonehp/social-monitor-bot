class CollectorError(RuntimeError):
    code = "collector_error"
    retryable = False


class RetryableCollectorError(CollectorError):
    code = "retryable"
    retryable = True


class NetworkCollectorError(RetryableCollectorError):
    code = "network"


class FeatureUnavailableError(CollectorError):
    code = "feature_unavailable"


class AccessDeniedError(CollectorError):
    code = "access_denied"


class NotFoundError(CollectorError):
    code = "not_found"


class CredentialDeadError(RetryableCollectorError):
    code = "credential_dead"


class ProxyUnavailableError(NetworkCollectorError):
    code = "proxy_unavailable"


class RateLimitedError(RetryableCollectorError):
    code = "rate_limited"

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after

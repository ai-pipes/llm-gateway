class GatewayError(Exception):
    pass


class SanitizerBlockedError(GatewayError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AdapterNotFoundError(GatewayError):
    def __init__(self, name: str | None):
        super().__init__(f"adapter_not_found:{name}")
        self.name = name


class UpstreamTimeoutError(GatewayError):
    pass


class UpstreamError(GatewayError):
    pass

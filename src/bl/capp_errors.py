"""Public CAPP failures contain only stable codes and HTTP metadata."""


class CappError(RuntimeError):
    def __init__(self, code: str, http_status: int | None = None,
                 retryable: bool = False) -> None:
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(f"CAPP {code}" + (f" (HTTP {http_status})" if http_status else ""))


class CappAuthenticationError(CappError):
    def __init__(self, http_status: int | None = None) -> None:
        super().__init__("authentication", http_status)


class CappConflictError(CappError):
    def __init__(self) -> None:
        super().__init__("conflict", 409)

"""Small adaptive concurrency state used only by OpenDART channels."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.errors import ErrorCode, SearchError


_SLOWDOWN_ERRORS = {
    ErrorCode.OPENDART_HTTP_RATE_LIMITED,
    ErrorCode.OPENDART_TEMPORARY_FAILURE,
}


@dataclass(slots=True)
class AdaptiveConcurrency:
    maximum: int
    current: int | None = None
    events: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.maximum < 1:
            raise ValueError("maximum must be positive")
        if self.current is None:
            self.current = self.maximum
        if not 1 <= self.current <= self.maximum:
            raise ValueError("current must be between 1 and maximum")

    def observe(self, error: SearchError) -> bool:
        # OpenDART status 020 has its own immediate-stop contract and must not
        # be confused with an adaptive, retryable HTTP slowdown signal.
        if error.code not in _SLOWDOWN_ERRORS or self.current <= 1:
            return False
        before = self.current
        self.current -= 1
        self.events.append({"reason": error.code.value, "from": before, "to": self.current})
        return True

"""Session-scoped channel circuit breaker."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from app.config.defaults import (
    NETWORK_CIRCUIT_SECONDS,
    NETWORK_FAILURE_THRESHOLD,
    STRUCTURE_CIRCUIT_SECONDS,
    STRUCTURE_FAILURE_THRESHOLD,
)
from app.contracts import ChannelStatus


@dataclass(slots=True)
class CircuitSnapshot:
    status: ChannelStatus = ChannelStatus.HEALTHY
    failure_class: str | None = None
    failure_count: int = 0
    blocked_until: float | None = None
    opened_count: int = 0


class CircuitBreaker:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.state = CircuitSnapshot()

    def before_request(self) -> ChannelStatus:
        if self.state.status == ChannelStatus.CIRCUIT_OPEN:
            if self.state.blocked_until is not None and self.clock() >= self.state.blocked_until:
                self.state.status = ChannelStatus.PROBING
            else:
                return ChannelStatus.CIRCUIT_OPEN
        return self.state.status

    def success(self) -> None:
        self.state.status = ChannelStatus.HEALTHY
        self.state.failure_class = None
        self.state.failure_count = 0
        self.state.blocked_until = None

    def failure(self, failure_class: str) -> ChannelStatus:
        if self.state.failure_class != failure_class:
            self.state.failure_count = 0
        self.state.failure_class = failure_class
        self.state.failure_count += 1
        threshold = STRUCTURE_FAILURE_THRESHOLD if failure_class == "structure_or_access" else NETWORK_FAILURE_THRESHOLD
        if self.state.status == ChannelStatus.PROBING or self.state.failure_count >= threshold:
            seconds = STRUCTURE_CIRCUIT_SECONDS if failure_class == "structure_or_access" else NETWORK_CIRCUIT_SECONDS
            self.state.status = ChannelStatus.CIRCUIT_OPEN
            self.state.blocked_until = self.clock() + seconds
            self.state.opened_count += 1
        else:
            self.state.status = ChannelStatus.DEGRADED
        return self.state.status

    def trip(self, failure_class: str) -> ChannelStatus:
        """Open the circuit for one already-confirmed failure event."""
        self.state.failure_class = failure_class
        self.state.failure_count = 1
        seconds = STRUCTURE_CIRCUIT_SECONDS if failure_class == "structure_or_access" else NETWORK_CIRCUIT_SECONDS
        self.state.status = ChannelStatus.CIRCUIT_OPEN
        self.state.blocked_until = self.clock() + seconds
        self.state.opened_count += 1
        return self.state.status

    def event(self) -> dict:
        return {
            "status": self.state.status.value,
            "failure_class": self.state.failure_class,
            "failure_count": self.state.failure_count,
            "blocked_until_epoch": self.state.blocked_until,
            "opened_count": self.state.opened_count,
        }

    def remaining_blocked_seconds(self) -> int:
        if self.state.status != ChannelStatus.CIRCUIT_OPEN or self.state.blocked_until is None:
            return 0
        return max(1, math.ceil(self.state.blocked_until - self.clock()))

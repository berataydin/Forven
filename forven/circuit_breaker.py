"""Circuit breaker helper for external service stability."""

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum


log = logging.getLogger("forven.circuit_breaker")


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Simple circuit-breaker with failure threshold and cooldown."""

    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    half_open_max_calls: int = 1

    state: State = field(default=State.CLOSED, init=False)
    failure_count: int = field(default=0, init=False)
    last_failure_time: float = field(default=0.0, init=False)
    half_open_calls: int = field(default=0, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def can_execute(self) -> bool:
        with self._lock:
            if self.state == State.CLOSED:
                return True

            if self.state == State.OPEN:
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = State.HALF_OPEN
                    self.half_open_calls = 0
                    log.info("circuit breaker '%s' -> HALF_OPEN", self.name)
                    return True
                return False

            if self.state == State.HALF_OPEN:
                if self.half_open_calls >= self.half_open_max_calls:
                    return False
                self.half_open_calls += 1
                return True

            return False

    def record_success(self) -> None:
        with self._lock:
            if self.state == State.HALF_OPEN:
                self.state = State.CLOSED
                self.half_open_calls = 0
            self.failure_count = 0

    def record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            self.half_open_calls = 0
            if self.state == State.HALF_OPEN:
                self.state = State.OPEN
                log.warning("circuit breaker '%s' -> OPEN (half-open test failed)", self.name)
                return

            if self.failure_count >= self.failure_threshold:
                self.state = State.OPEN
                log.warning("circuit breaker '%s' -> OPEN (%d failures)", self.name, self.failure_count)


# Shared HyperLiquid breakers (prices/trades/account) used by exchange adapters.
hl_price_breaker = CircuitBreaker(
    name="hl_price",
    failure_threshold=5,
    recovery_timeout=20.0,
    half_open_max_calls=2,
)
hl_trade_breaker = CircuitBreaker(
    name="hl_trade",
    failure_threshold=3,
    recovery_timeout=30.0,
    half_open_max_calls=1,
)
hl_account_breaker = CircuitBreaker(
    name="hl_account",
    failure_threshold=4,
    recovery_timeout=25.0,
    half_open_max_calls=1,
)

# Propr.xyz prop-firm API breakers (trade = order placement/cancel, account =
# positions/attempts/balance reads). Prices are NOT a Propr concern — the
# adapter reads mids from Hyperliquid (Propr executes on HL markets).
propr_trade_breaker = CircuitBreaker(
    name="propr_trade",
    failure_threshold=3,
    recovery_timeout=30.0,
    half_open_max_calls=1,
)
propr_account_breaker = CircuitBreaker(
    name="propr_account",
    failure_threshold=4,
    recovery_timeout=25.0,
    half_open_max_calls=1,
)

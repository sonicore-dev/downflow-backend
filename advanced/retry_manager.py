# Bounded retry with exponential backoff for transient extraction failures.

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from .error_handler import ErrorCode, ExtractionError, classify_exception

__all__ = ["RetryPolicy", "run_with_retries"]

log = logging.getLogger("downflow.retry")
T = TypeVar("T")

# Only these are ever retried. Permanent failures (private, deleted, auth,
# geo, unsupported, ...) are raised immediately.
RETRYABLE_CODES = frozenset({ErrorCode.RATE_LIMITED, ErrorCode.TIMEOUT, ErrorCode.NETWORK_ERROR})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3          # total tries, including the first
    base_delay: float = 1.5        # seconds
    max_delay: float = 20.0        # cap for a single sleep
    backoff_factor: float = 2.0
    jitter: float = 0.25           # +/- fraction of the delay
    rate_limit_max_attempts: int = 2

    def delay_for(self, attempt: int, error: ExtractionError) -> float:
        delay = min(self.max_delay, self.base_delay * (self.backoff_factor ** (attempt - 1)))
        if error.retry_after:
            delay = max(delay, min(float(error.retry_after), self.max_delay))
        spread = delay * self.jitter
        return max(0.0, delay + random.uniform(-spread, spread))


def run_with_retries(
    func: Callable[[], T],
    policy: RetryPolicy,
    deadline: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Optional[Callable[[ExtractionError, int, float], None]] = None,
) -> T:
    # Call ``func`` retrying transient errors within ``policy`` and ``deadline``.
    #
    # ``deadline`` is an absolute ``time.monotonic()`` value; no retry is started
    # if its backoff would run past it.
    attempt = 0
    while True:
        attempt += 1
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            error = classify_exception(exc)
            limit = policy.rate_limit_max_attempts if error.code is ErrorCode.RATE_LIMITED else policy.max_attempts
            if error.code not in RETRYABLE_CODES or attempt >= limit:
                raise error from exc
            delay = policy.delay_for(attempt, error)
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise error from exc
            log.warning("transient failure (%s), retry %d/%d in %.1fs: %s",
                        error.code.value, attempt, limit - 1, delay, error.detail)
            if on_retry is not None:
                try:
                    on_retry(error, attempt, delay)
                except Exception:  # noqa: BLE001 - reporting must never break extraction
                    pass
            sleep(delay)

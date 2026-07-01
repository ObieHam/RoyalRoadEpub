"""
Polite rate limiter for scraping RoyalRoad.

Design goals:
- Never hammer the server: enforce a minimum delay + random jitter between
  every request, regardless of what happens.
- Respect the server when it asks us to slow down: honor `Retry-After`
  headers and back off exponentially on 429 / 503 responses.
- Fail closed: if the server keeps telling us to slow down after several
  tries, give up on that request rather than looping forever.
"""

import random
import time
import threading


class RateLimiter:
    def __init__(self, min_delay=2.5, jitter=1.5, max_backoff=90, max_retries=5):
        self.min_delay = min_delay
        self.jitter = jitter
        self.max_backoff = max_backoff
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._last_request_time = 0.0

    def _wait_for_slot(self):
        """Block until enough time has passed since the last request."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            delay = self.min_delay + random.uniform(0, self.jitter)
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self._last_request_time = time.monotonic()

    def request(self, fetch_fn, description="request"):
        """
        Run fetch_fn() (a zero-arg callable that performs one HTTP request
        and returns a `requests.Response`), applying the pacing delay first
        and retrying with exponential backoff if the server signals it's
        being overwhelmed (429/503).

        Raises the last exception/response error if all retries are
        exhausted.
        """
        backoff = 5.0
        last_exc = None

        for attempt in range(1, self.max_retries + 1):
            self._wait_for_slot()
            try:
                response = fetch_fn()
            except Exception as exc:  # network errors, timeouts, etc.
                last_exc = exc
                sleep_for = min(backoff, self.max_backoff)
                time.sleep(sleep_for)
                backoff *= 2
                continue

            if response.status_code in (429, 503):
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_for = min(float(retry_after), self.max_backoff)
                    except ValueError:
                        sleep_for = min(backoff, self.max_backoff)
                else:
                    sleep_for = min(backoff, self.max_backoff)
                last_exc = RuntimeError(
                    f"Rate limited ({response.status_code}) on {description}, "
                    f"backing off {sleep_for:.0f}s (attempt {attempt}/{self.max_retries})"
                )
                time.sleep(sleep_for)
                backoff *= 2
                continue

            return response

        raise RuntimeError(
            f"Gave up on {description} after {self.max_retries} attempts: {last_exc}"
        )

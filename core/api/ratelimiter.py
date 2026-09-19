import asyncio
import time
from collections import deque


class RateLimiter:
    """Rolling request cap with queuing for when you overshoot."""

    def __init__(self, max_requests: int = 48, per_seconds: float = 30.0):
        """
        :param max_requests: max requests allowed within the rolling window
        :param per_seconds: length of the rolling window, in seconds
        """
        self.max_requests = max_requests
        self.per_seconds = per_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a request slot is free under the rolling window, then reserve it."""
        async with self._lock:
            while True:
                now = time.monotonic()
                while (
                    self._timestamps and now - self._timestamps[0] >= self.per_seconds
                ):
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                await asyncio.sleep(self.per_seconds - (now - self._timestamps[0]))

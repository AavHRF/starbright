from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterable, AsyncIterator, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

SSE_URL = "https://www.nationstates.net/api/"

ALL_BUCKET = "all"

_MIN_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
# The 429 messages describe connection limits
_RATE_LIMIT_BACKOFF = 60.0
# The server sends a heartbeat comment every 15-20s
_IDLE_TIMEOUT = 90.0
_DEDUPE_WINDOW = 5000


@dataclass
class RawEvent:
    """One happening exactly as the SSE feed delivered it."""

    id: int
    time: int
    text: str
    html: str
    buckets: list[str]
    rmb_message: Optional[str] = None


@dataclass
class ReplayGap:
    """Sent when the requested Last-Event-ID had already fallen out of the server's replay buffer."""

    requested_id: Optional[int]
    oldest_available_id: Optional[int]
    latest_available_id: Optional[int]
    reason: str


class _RateLimited(Exception):
    """The SSE endpoint answered 429."""


async def parse_stream(
    lines: AsyncIterable[bytes],
) -> AsyncIterator[tuple[str, Optional[str], str]]:
    """Split a text/event-stream body into its events; comment lines (heartbeats) are ignored.
    :param lines: the body, one raw line at a time
    :return: (event name, SSE id or None, data) for each complete event
    """
    name, event_id, data = "message", None, []
    async for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                yield name, event_id, "\n".join(data)
            name, event_id, data = "message", None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data.append(value)


def _to_int(value: object) -> Optional[int]:
    """Convert a JSON value the feed may send as either a string or a number.
    :param value: the value to convert
    :return: the integer, or None if it is missing or not numeric
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class SseClient:
    """A self-healing connection to the NationStates SSE feed that hands each new happening to a callback."""

    def __init__(
        self,
        user_agent: str,
        on_event: Callable[[RawEvent], None],
        on_gap: Callable[[ReplayGap], None],
        *,
        buckets: str = ALL_BUCKET,
        last_event_id: Optional[int] = None,
    ):
        """
        :param user_agent: contact string sent as the User-Agent header, per NS API policy
        :param on_event: called once per happening, in arrival order; must not block
        :param on_gap: called when the server reports it can't replay everything since last_event_id
        :param buckets: the "+"-joined buckets to subscribe to
        :param last_event_id: the newest happening already processed, so the feed can replay what was missed
        """
        self.user_agent = user_agent
        self.buckets = buckets
        self.last_event_id = last_event_id
        self._on_event = on_event
        self._on_gap = on_gap
        self._recent: deque[int] = deque()
        self._recent_set: set[int] = set()
        # Set once a connection is accepted, so a drop after a healthy stretch retries quickly.
        self._accepted = False

    def _is_duplicate(self, event_id: int) -> bool:
        """Record an event ID and report whether it was already seen recently (replays can repeat events).
        :param event_id: the happening ID
        :return: True if it was seen before
        """
        if event_id in self._recent_set:
            return True
        self._recent.append(event_id)
        self._recent_set.add(event_id)
        if len(self._recent) > _DEDUPE_WINDOW:
            self._recent_set.discard(self._recent.popleft())
        return False

    def _handle(self, name: str, data: str) -> None:
        """Decode one event and pass it to the matching callback.
        :param name: the SSE event name, "message" for ordinary happenings
        :param data: the event's JSON payload
        """
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Ignoring SSE event with malformed JSON: %.200s", data)
            return # I give up

        if name == "replay-gap":
            self._on_gap(
                ReplayGap(
                    requested_id=_to_int(payload.get("requestedId")),
                    oldest_available_id=_to_int(payload.get("oldestAvailableId")),
                    latest_available_id=_to_int(payload.get("latestAvailableId")),
                    reason=str(payload.get("reason", "")),
                )
            )
            return

        event_id, when = _to_int(payload.get("id")), _to_int(payload.get("time"))
        if event_id is None or when is None:
            logger.warning("Ignoring SSE event without an id/time: %.200s", data)
            return
        if self._is_duplicate(event_id):
            return
        self.last_event_id = max(self.last_event_id or 0, event_id)
        self._on_event(
            RawEvent(
                id=event_id,
                time=when,
                text=payload.get("str", ""),
                html=payload.get("htmlStr", ""),
                buckets=list(payload.get("buckets", [])),
                rmb_message=payload.get("rmbMessage"),
            )
        )

    async def _connect_once(self, session: aiohttp.ClientSession) -> None:
        """Hold one connection open until it drops.
        :param session: the HTTP session to use
        """
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if self.last_event_id is not None:
            headers["Last-Event-ID"] = str(self.last_event_id)
        async with session.get(SSE_URL + self.buckets, headers=headers) as resp:
            if resp.status == 429:
                raise _RateLimited(await resp.text())
            resp.raise_for_status()
            self._accepted = True
            logger.info("Connected to SSE feed (%s)", self.buckets)
            async for name, _, data in parse_stream(resp.content):
                self._handle(name, data)

    async def run(self) -> None:
        """Stay connected until cancelled, reconnecting with backoff whenever the stream drops."""
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=15, sock_read=_IDLE_TIMEOUT
        )
        backoff = _MIN_BACKOFF
        async with aiohttp.ClientSession(
            headers={"User-Agent": self.user_agent},
            timeout=timeout,
            read_bufsize=2**20,
        ) as session:
            while True:
                self._accepted = False
                try:
                    await self._connect_once(session)
                    logger.warning("SSE stream ended, reconnecting")
                    backoff = _MIN_BACKOFF
                except _RateLimited as exc:
                    logger.warning("SSE connection refused (429): %s", exc)
                    backoff = _RATE_LIMIT_BACKOFF
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    if self._accepted:
                        backoff = _MIN_BACKOFF
                    logger.warning("SSE connection lost (%r), retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                if not self._accepted: # ????
                    backoff = min(backoff * 2, _MAX_BACKOFF)

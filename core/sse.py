from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Collection, Optional, Union

from core.api.sse import RawEvent, ReplayGap, SseClient
from core.database import Database
from core.models.sse_event import SseEvent
from core.permissions import PermissionTier
from core.settings import SettingDefinition, SettingsRegistry, SettingType, SettingValue

logger = logging.getLogger(__name__)

EventCallback = Callable[[SseEvent], Union[None, Awaitable[None]]]
GapCallback = Callable[[ReplayGap], Union[None, Awaitable[None]]]

_RETENTION_KEY = "sse.retention_hours"
_DEFAULT_RETENTION_HOURS = 72
_PRUNE_INTERVAL = 3600.0
# After the first event of a burst arrives, wait this long so the rest can be stored in the same insert.
_BATCH_WINDOW = 0.25
_MAX_BATCH = 1000


def _non_negative(value: SettingValue) -> SettingValue:
    """Check that a retention period is not negative.
    :param value: the entered number of hours
    :return: the same value
    """
    if value < 0:  # type: ignore[operator]
        raise ValueError("Retention can't be negative. Use 0 to keep events forever.")
    return value


class SseFeed:
    """Follows the NationStates SSE "all" feed, stores every happening, and hands each one to subscribers.

    Other modules use it two ways: ``subscribe`` to react to happenings as they arrive, and the SseEvent
    model to query the stored history.
    """

    def __init__(self, db: Database, user_agent: str, settings: SettingsRegistry):
        """
        :param db: core Database wrapper
        :param user_agent: contact string sent as the User-Agent header, per NS API policy
        :param settings: the core settings registry, to register the retention setting in
        """
        self._db = db
        self._user_agent = user_agent
        self._settings = settings
        self._subscribers: list[tuple[EventCallback, Optional[frozenset[str]]]] = []
        self._gap_subscribers: list[GapCallback] = []
        self._queue: asyncio.Queue[Union[SseEvent, ReplayGap]] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self.last_event_at: Optional[datetime] = None

        settings.register(
            SettingDefinition(
                key=_RETENTION_KEY,
                category="SSE Feed",
                label="Event retention (hours)",
                description="How long stored NationStates happenings are kept. 0 keeps them forever.",
                type=SettingType.INTEGER,
                default=_DEFAULT_RETENTION_HOURS,
                min_tier=PermissionTier.ADMINISTRATOR,
                normalize=_non_negative,
            )
        )

    def subscribe(
        self, callback: EventCallback, buckets: Optional[Collection[str]] = None
    ) -> Callable[[], None]:
        """Have a callback run for each new happening, after it has been stored.
        :param callback: sync or async function taking an SseEvent; exceptions are logged, not raised
        :param buckets: only events in at least one of these buckets, e.g. {"move", "region:balder"}; None for all
        :return: a function that cancels the subscription
        """
        entry = (callback, frozenset(buckets) if buckets is not None else None)
        self._subscribers.append(entry)
        return lambda: self._subscribers.remove(entry) if entry in self._subscribers else None

    def on_gap(self, callback: GapCallback) -> Callable[[], None]:
        """Have a callback run when the feed reports it couldn't replay every event missed while disconnected.
        :param callback: sync or async function taking a ReplayGap
        :return: a function that cancels the subscription
        """
        self._gap_subscribers.append(callback)
        return lambda: self._gap_subscribers.remove(callback) if callback in self._gap_subscribers else None

    async def start(self) -> None:
        """Begin following the feed, resuming from the newest stored event."""
        client = SseClient(
            self._user_agent,
            on_event=lambda raw: self._queue.put_nowait(SseEvent.from_raw(raw)),
            on_gap=self._queue.put_nowait,
            last_event_id=await SseEvent.latest_id(self._db),
        )
        self._tasks = [
            asyncio.create_task(client.run(), name="sse-client"),
            asyncio.create_task(self._worker(), name="sse-worker"),
            asyncio.create_task(self._prune_loop(), name="sse-prune"),
        ]

    async def stop(self) -> None:
        """Stop following the feed."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _call(self, callback: Callable, arg: object) -> None:
        """Run a subscriber, containing any failure so one bad subscriber can't disrupt the feed.
        :param callback: the subscriber
        :param arg: what to pass it
        """
        try:
            result = callback(arg)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("SSE subscriber %r failed", callback)

    async def _worker(self) -> None:
        """Store queued events in batches, then deliver them (and gap notices) to subscribers in order."""
        while True:
            batch = [await self._queue.get()]
            await asyncio.sleep(_BATCH_WINDOW)
            while len(batch) < _MAX_BATCH and not self._queue.empty():
                batch.append(self._queue.get_nowait())

            events = [item for item in batch if isinstance(item, SseEvent)]
            if events:
                self.last_event_at = datetime.now(timezone.utc)
                try:
                    await SseEvent.insert_many(self._db, events)
                except Exception:
                    logger.exception("Failed to store %d SSE events", len(events))

            for item in batch:
                if isinstance(item, ReplayGap):
                    logger.warning("SSE replay gap: %s", item)
                    for callback in list(self._gap_subscribers):
                        await self._call(callback, item)
                    continue
                kinds = set(item.buckets)
                for callback, wanted in list(self._subscribers):
                    if wanted is None or wanted & kinds:
                        await self._call(callback, item)

    async def _prune_loop(self) -> None:
        """Periodically delete stored events older than the retention setting."""
        while True:
            hours = self._settings.get(_RETENTION_KEY)
            if hours:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
                try:
                    deleted = await SseEvent.prune(self._db, cutoff)
                    if deleted:
                        logger.info("Pruned %d stored SSE events", deleted)
                except Exception:
                    logger.exception("Failed to prune SSE events")
            await asyncio.sleep(_PRUNE_INTERVAL)

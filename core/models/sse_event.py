from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Literal, Optional

import asyncpg

if TYPE_CHECKING:
    from core.api.sse import RawEvent
    from core.database import Database

_RELOCATED_RE = re.compile(
    r"^@@(?P<nation>[^@]+)@@ relocated from %%(?P<origin>[^%]+)%% to %%(?P<destination>[^%]+)%%\.?$"
)
_FOUNDED_RE = re.compile(
    r"^@@(?P<nation>[^@]+)@@ was (?P<re>re)?founded in %%(?P<destination>[^%]+)%%\.?$"
)

_COLUMNS = "id, time, text, buckets, rmb_message"


@dataclass(frozen=True)
class Movement:
    """A nation entering a region, read from a move or founding happening. Names are NS canonical IDs."""

    nation: str
    origin: Optional[str]
    destination: str
    kind: Literal["relocated", "founded", "refounded"]


@dataclass
class SseEvent:
    """ORM-style representation of a row in the ns_events table: one NationStates happening.

    Other modules read the feed through these queries, or subscribe live with SseFeed.subscribe.
    ``text`` keeps NationStates' markup: nations appear as ``@@nation_id@@`` and regions as ``%%region_id%%``.
    """

    id: int
    time: datetime
    text: str
    buckets: list[str]
    rmb_message: Optional[str] = None

    @property
    def kinds(self) -> list[str]:
        """The event categories, e.g. ``move`` or ``rmb``: every bucket except ``all`` and the scoped ones."""
        return [b for b in self.buckets if b != "all" and ":" not in b]

    @property
    def nations(self) -> list[str]:
        """Canonical IDs of the nations the event concerns."""
        return [b.removeprefix("nation:") for b in self.buckets if b.startswith("nation:")]

    @property
    def regions(self) -> list[str]:
        """Canonical IDs of the regions the event concerns."""
        return [b.removeprefix("region:") for b in self.buckets if b.startswith("region:")]

    @property
    def movement(self) -> Optional[Movement]:
        """The nation movement this event describes, or None if it isn't a move/founding happening."""
        match = _RELOCATED_RE.match(self.text)
        if match:
            return Movement(
                match["nation"], match["origin"], match["destination"], "relocated"
            )
        match = _FOUNDED_RE.match(self.text)
        if match:
            kind = "refounded" if match["re"] else "founded"
            return Movement(match["nation"], None, match["destination"], kind)
        return None

    @classmethod
    def from_raw(cls, raw: "RawEvent") -> "SseEvent":
        """Convert an event straight off the feed into a model instance.
        :param raw: the event as delivered
        :return: the equivalent SseEvent (not yet stored)
        """
        return cls(
            id=raw.id,
            time=datetime.fromtimestamp(raw.time, timezone.utc),
            text=html.unescape(raw.text),
            buckets=raw.buckets,
            rmb_message=raw.rmb_message,
        )

    @classmethod
    def _from_record(cls, record: asyncpg.Record) -> "SseEvent":
        """Build an SseEvent from a database record.
        :param record: row returned by asyncpg
        :return: populated SseEvent instance
        """
        return cls(
            id=record["id"],
            time=record["time"],
            text=record["text"],
            buckets=list(record["buckets"]),
            rmb_message=record["rmb_message"],
        )

    @classmethod
    async def insert_many(cls, db: "Database", events: Iterable["SseEvent"]) -> None:
        """Store events, skipping any already stored.
        :param db: core Database wrapper
        :param events: the events to store
        """
        await db.executemany(
            """
            INSERT INTO ns_events (id, time, text, buckets, rmb_message)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO NOTHING
            """,
            [(e.id, e.time, e.text, e.buckets, e.rmb_message) for e in events],
        )

    @classmethod
    async def get(cls, db: "Database", event_id: int) -> Optional["SseEvent"]:
        """Fetch one event by its happening ID.
        :param db: core Database wrapper
        :param event_id: NationStates happening ID
        :return: matching SseEvent, or None
        """
        record = await db.fetchrow(
            f"SELECT {_COLUMNS} FROM ns_events WHERE id = $1", event_id
        )
        return cls._from_record(record) if record else None

    @classmethod
    async def latest_id(cls, db: "Database") -> Optional[int]:
        """Find the newest stored happening ID.
        :param db: core Database wrapper
        :return: the highest stored ID, or None if nothing is stored
        """
        return await db.fetchval("SELECT max(id) FROM ns_events")

    @classmethod
    async def query(
        cls,
        db: "Database",
        *,
        any_of: Optional[list[str]] = None,
        after_id: Optional[int] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
        newest_first: bool = True,
    ) -> list["SseEvent"]:
        """Search stored events.
        :param db: core Database wrapper
        :param any_of: only events in at least one of these buckets, e.g. ["move", "region:balder"]
        :param after_id: only events with a higher happening ID than this
        :param since: only events at or after this time
        :param limit: maximum number of events to return
        :param newest_first: order by newest first rather than oldest first
        :return: the matching events
        """
        order = "DESC" if newest_first else "ASC"
        records = await db.fetch(
            f"""
            SELECT {_COLUMNS} FROM ns_events
            WHERE ($1::text[] IS NULL OR buckets && $1::text[])
              AND ($2::bigint IS NULL OR id > $2)
              AND ($3::timestamptz IS NULL OR time >= $3)
            ORDER BY id {order}
            LIMIT $4
            """,
            any_of,
            after_id,
            since,
            limit,
        )
        return [cls._from_record(r) for r in records]

    @classmethod
    async def for_region(
        cls, db: "Database", region: str, *, limit: int = 100, after_id: Optional[int] = None
    ) -> list["SseEvent"]:
        """Fetch the newest events concerning a region.
        :param db: core Database wrapper
        :param region: canonical region ID (lowercase, underscores)
        :param limit: maximum number of events to return
        :param after_id: only events with a higher happening ID than this
        :return: matching events, newest first
        """
        return await cls.query(
            db, any_of=[f"region:{region}"], limit=limit, after_id=after_id
        )

    @classmethod
    async def for_nation(
        cls, db: "Database", nation: str, *, limit: int = 100, after_id: Optional[int] = None
    ) -> list["SseEvent"]:
        """Fetch the newest events concerning a nation.
        :param db: core Database wrapper
        :param nation: canonical nation ID (lowercase, underscores)
        :param limit: maximum number of events to return
        :param after_id: only events with a higher happening ID than this
        :return: matching events, newest first
        """
        return await cls.query(
            db, any_of=[f"nation:{nation}"], limit=limit, after_id=after_id
        )

    @classmethod
    async def prune(cls, db: "Database", before: datetime) -> int:
        """Delete events older than a cutoff.
        :param db: core Database wrapper
        :param before: events with an earlier time than this are deleted
        :return: how many events were deleted
        """
        status = await db.execute("DELETE FROM ns_events WHERE time < $1", before)
        return int(status.rsplit(" ", 1)[-1])

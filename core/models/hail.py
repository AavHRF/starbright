from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Optional

import asyncpg

if TYPE_CHECKING:
    from core.database import Database


def canonical(name: str) -> str:
    """Normalise a nation or region name to NationStates' canonical ID form.
    :param name: a name as a person would type it, e.g. "The Pacific"
    :return: the lowercase, underscore-separated ID, e.g. "the_pacific"
    """
    return name.strip().lower().replace(" ", "_")


def display_name(canonical_id: str) -> str:
    """Turn a canonical ID back into a readable name (capitalisation is approximate).
    :param canonical_id: e.g. "the_pacific"
    :return: e.g. "The Pacific"
    """
    return canonical_id.replace("_", " ").title()


@dataclass
class HailArrival:
    """ORM-style representation of a row in the hail_arrivals table: a nation first seen in a region."""

    id: int
    region: str
    nation: str
    arrived_at: datetime
    event_id: Optional[int] = None

    @classmethod
    def _from_record(cls, record: asyncpg.Record) -> "HailArrival":
        """Build a HailArrival from a database record.
        :param record: row returned by asyncpg
        :return: populated HailArrival instance
        """
        return cls(
            id=record["id"],
            region=record["region"],
            nation=record["nation"],
            arrived_at=record["arrived_at"],
            event_id=record["event_id"],
        )

    @classmethod
    async def record(
        cls,
        db: "Database",
        region: str,
        nation: str,
        arrived_at: datetime,
        event_id: Optional[int] = None,
    ) -> bool:
        """Record a nation as seen in a region, unless it has been seen there before.
        :param db: core Database wrapper
        :param region: canonical region ID; must be a tracked region
        :param nation: canonical nation ID
        :param arrived_at: when the nation arrived
        :param event_id: the SSE happening that announced it, if any
        :return: True if this is the first time the nation was seen in the region
        """
        status = await db.execute(
            """
            INSERT INTO hail_arrivals (region, nation, arrived_at, event_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (region, nation) DO NOTHING
            """,
            region,
            nation,
            arrived_at,
            event_id,
        )
        return status.endswith(" 1")

    @classmethod
    async def record_many(
        cls, db: "Database", region: str, nations: Iterable[str], arrived_at: datetime
    ) -> None:
        """Record several nations as seen in a region, skipping ones already seen.
        :param db: core Database wrapper
        :param region: canonical region ID; must be a tracked region
        :param nations: canonical nation IDs
        :param arrived_at: the arrival time to give the new rows
        """
        await db.executemany(
            """
            INSERT INTO hail_arrivals (region, nation, arrived_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (region, nation) DO NOTHING
            """,
            [(region, nation, arrived_at) for nation in nations],
        )

    @classmethod
    async def unlisted(cls, db: "Database", region: str) -> list["HailArrival"]:
        """Fetch arrivals that /hail hasn't listed yet.
        :param db: core Database wrapper
        :param region: canonical region ID
        :return: the region's unlisted arrivals, oldest first
        """
        records = await db.fetch(
            """
            SELECT a.* FROM hail_arrivals a
            JOIN hail_regions r ON r.region = a.region
            WHERE a.region = $1 AND a.id > r.last_listed_id
            ORDER BY a.id
            """,
            region,
        )
        return [cls._from_record(r) for r in records]


@dataclass
class HailRegion:
    """ORM-style representation of a row in the hail_regions table: a region whose arrivals are tracked."""

    region: str
    last_listed_id: int = 0

    @classmethod
    def _from_record(cls, record: asyncpg.Record) -> "HailRegion":
        """Build a HailRegion from a database record.
        :param record: row returned by asyncpg
        :return: populated HailRegion instance
        """
        return cls(region=record["region"], last_listed_id=record["last_listed_id"])

    @classmethod
    async def all(cls, db: "Database") -> list["HailRegion"]:
        """Fetch every tracked region.
        :param db: core Database wrapper
        :return: the tracked regions, alphabetically
        """
        records = await db.fetch("SELECT * FROM hail_regions ORDER BY region")
        return [cls._from_record(r) for r in records]

    @classmethod
    async def waiting_counts(cls, db: "Database") -> dict[str, int]:
        """Count each tracked region's arrivals that /hail hasn't listed yet.
        :param db: core Database wrapper
        :return: region -> number of unlisted arrivals, including regions with none
        """
        records = await db.fetch(
            """
            SELECT r.region, count(a.id) AS waiting
            FROM hail_regions r
            LEFT JOIN hail_arrivals a ON a.region = r.region AND a.id > r.last_listed_id
            GROUP BY r.region
            """
        )
        return {r["region"]: r["waiting"] for r in records}

    @classmethod
    async def get(cls, db: "Database", region: str) -> Optional["HailRegion"]:
        """Fetch a tracked region.
        :param db: core Database wrapper
        :param region: canonical region ID
        :return: the region, or None if it isn't tracked
        """
        record = await db.fetchrow(
            "SELECT * FROM hail_regions WHERE region = $1", region
        )
        return cls._from_record(record) if record else None

    @classmethod
    async def seed(
        cls, db: "Database", region: str, residents: Iterable[str], now: datetime
    ) -> "HailRegion":
        """Start tracking a region, or restart it: the current residents become seen and nothing is left to list.
        :param db: core Database wrapper
        :param region: canonical region ID
        :param residents: canonical IDs of the nations in the region right now
        :param now: the time to give the residents' rows
        :return: the tracked region
        """
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO hail_regions (region) VALUES ($1) ON CONFLICT DO NOTHING",
                region,
            )
            await conn.execute("DELETE FROM hail_arrivals WHERE region = $1", region)
            await conn.executemany(
                "INSERT INTO hail_arrivals (region, nation, arrived_at) VALUES ($1, $2, $3)",
                [(region, nation, now) for nation in set(residents)],
            )
            record = await conn.fetchrow(
                """
                UPDATE hail_regions
                SET last_listed_id = coalesce((SELECT max(id) FROM hail_arrivals WHERE region = $1), last_listed_id)
                WHERE region = $1
                RETURNING *
                """,
                region,
            )
        return cls._from_record(record)

    async def mark_listed(self, db: "Database", up_to_id: int) -> None:
        """Record that arrivals up to an ID have been listed.
        :param db: core Database wrapper
        :param up_to_id: the newest hail_arrivals.id that was listed
        """
        await db.execute(
            "UPDATE hail_regions SET last_listed_id = greatest(last_listed_id, $2) WHERE region = $1",
            self.region,
            up_to_id,
        )
        self.last_listed_id = max(self.last_listed_id, up_to_id)

    async def delete(self, db: "Database") -> None:
        """Stop tracking the region and forget its arrivals.
        :param db: core Database wrapper
        """
        await db.execute("DELETE FROM hail_regions WHERE region = $1", self.region)

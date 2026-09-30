from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import asyncpg

if TYPE_CHECKING:
    from core.database import Database


@dataclass
class Starborn:
    """ORM-style representation of a row in the stl_members table."""

    st_id: Optional[int]
    discord_id: int
    stl_nation: str = ""
    hzn_nation: str = ""
    status: str = "voyager"

    @classmethod
    def _from_record(cls, record: asyncpg.Record) -> "Starborn":
        """Build a Starborn instance from a database record.
        :param record: row returned by asyncpg
        :return: populated Starborn instance
        """
        return cls(
            st_id=record["st_id"],
            discord_id=record["discord_id"],
            stl_nation=record["stl_nation"],
            hzn_nation=record["hzn_nation"],
            status=record["status"],
        )

    @classmethod
    async def create(
        cls, db: "Database", discord_id: int, stl_nation: str = "", hzn_nation: str = ""
    ) -> "Starborn":
        """Insert a new member record, with the default status, and return it.
        :param db: core Database wrapper
        :param discord_id: Discord user ID of the applicant
        :param stl_nation: nation name in Starlight, if any
        :param hzn_nation: nation name in Horizon, if any
        :return: the newly created Starborn instance
        """
        record = await db.fetchrow(
            """
            INSERT INTO stl_members (discord_id, stl_nation, hzn_nation)
            VALUES ($1, $2, $3)
            RETURNING st_id, discord_id, stl_nation, hzn_nation, status
            """,
            discord_id,
            stl_nation,
            hzn_nation,
        )
        return cls._from_record(record)

    @classmethod
    async def get_by_id(cls, db: "Database", st_id: int) -> Optional["Starborn"]:
        """Fetch a member record by its internal ID.
        :param db: core Database wrapper
        :param st_id: internal member record ID
        :return: matching Starborn instance, or None
        """
        record = await db.fetchrow("SELECT * FROM stl_members WHERE st_id = $1", st_id)
        return cls._from_record(record) if record else None

    @classmethod
    async def get_by_discord_id(
        cls, db: "Database", discord_id: int
    ) -> Optional["Starborn"]:
        """Fetch a member record by Discord user ID.
        :param db: core Database wrapper
        :param discord_id: Discord user ID to look up
        :return: matching Starborn instance, or None
        """
        record = await db.fetchrow(
            "SELECT * FROM stl_members WHERE discord_id = $1", discord_id
        )
        return cls._from_record(record) if record else None

    async def save(self, db: "Database") -> None:
        """Persist this instance's current field values to the database.
        :param db: core Database wrapper
        """
        await db.execute(
            """
            UPDATE stl_members
            SET discord_id = $2, stl_nation = $3, hzn_nation = $4, status = $5
            WHERE st_id = $1
            """,
            self.st_id,
            self.discord_id,
            self.stl_nation,
            self.hzn_nation,
            self.status,
        )

    async def delete(self, db: "Database") -> None:
        """Delete this record from the database.
        :param db: core Database wrapper
        """
        await db.execute("DELETE FROM stl_members WHERE st_id = $1", self.st_id)

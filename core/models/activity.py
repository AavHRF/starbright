from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import asyncpg

if TYPE_CHECKING:
    from core.database import Database


@dataclass
class StbActivity:
    """ORM-style representation of a row in the stb_activity table."""

    userid: int
    wa_status: bool = False
    last_ns_login: Optional[datetime] = None
    last_discord_message_sent: Optional[datetime] = None
    last_rmb_message_sent: Optional[datetime] = None

    @classmethod
    def _from_record(cls, record: asyncpg.Record) -> "StbActivity":
        """Build a StbActivity instance from a database record.
        :param record: row returned by asyncpg
        :return: populated StbActivity instance
        """
        return cls(
            userid=record["userid"],
            wa_status=record["wa_status"],
            last_ns_login=record["last_ns_login"],
            last_discord_message_sent=record["last_discord_message_sent"],
            last_rmb_message_sent=record["last_rmb_message_sent"],
        )

    @classmethod
    async def create(cls, db: "Database", userid: int) -> "StbActivity":
        """Insert a new activity record for an existing starborn user.
        :param db: core Database wrapper
        :param userid: st_id of the starborn record this activity row belongs to
        :return: the newly created StbActivity instance
        """
        record = await db.fetchrow(
            """
            INSERT INTO stb_activity (userid)
            VALUES ($1)
            RETURNING userid, wa_status, last_ns_login, last_discord_message_sent, last_rmb_message_sent
            """,
            userid,
        )
        return cls._from_record(record)

    @classmethod
    async def get_by_userid(
        cls, db: "Database", userid: int
    ) -> Optional["StbActivity"]:
        """Fetch an activity record by its owning starborn ID.
        :param db: core Database wrapper
        :param userid: st_id of the owning starborn record
        :return: matching StbActivity instance, or None
        """
        record = await db.fetchrow(
            "SELECT * FROM stb_activity WHERE userid = $1", userid
        )
        return cls._from_record(record) if record else None

    async def save(self, db: "Database") -> None:
        """Persist this instance's current field values to the database.
        :param db: core Database wrapper
        """
        await db.execute(
            """
            UPDATE stb_activity
            SET wa_status = $2,
                last_ns_login = $3,
                last_discord_message_sent = $4,
                last_rmb_message_sent = $5
            WHERE userid = $1
            """,
            self.userid,
            self.wa_status,
            self.last_ns_login,
            self.last_discord_message_sent,
            self.last_rmb_message_sent,
        )

    async def delete(self, db: "Database") -> None:
        """Delete this record from the database.
        :param db: core Database wrapper
        """
        await db.execute("DELETE FROM stb_activity WHERE userid = $1", self.userid)

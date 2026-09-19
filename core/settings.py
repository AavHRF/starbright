from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional, Union

from core.database import Database

SettingValue = Union[bool, int, float, str, list[int], None]


class SettingType(enum.Enum):
    """The kind of value a setting holds, which determines which UI control edits it."""

    BOOLEAN = "boolean"
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    CHOICE = "choice"
    CHANNEL = "channel"
    ROLE = "role"
    USER = "user"
    MULTI_ROLE = "multi_role"
    MULTI_USER = "multi_user"


_INT_LIKE = (
    SettingType.INTEGER,
    SettingType.CHANNEL,
    SettingType.ROLE,
    SettingType.USER,
)
_LIST_LIKE = (SettingType.MULTI_ROLE, SettingType.MULTI_USER)


@dataclass
class SettingDefinition:
    """Describes one cog-registered setting: its type, default, and how to display it."""

    key: str
    category: str
    label: str
    description: str
    type: SettingType
    default: SettingValue = None
    choices: Optional[list[str]] = None
    min_tier: Optional[int] = None

    def cast(self, raw: str) -> SettingValue:
        """Cast a raw string pulled from the database to this setting's Python type.
        :param raw: the stored string value
        :return: the value cast to bool/int/float/str/list[int]
        """
        if self.type is SettingType.BOOLEAN:
            return raw == "true"
        if self.type in _INT_LIKE:
            return int(raw)
        if self.type is SettingType.FLOAT:
            return float(raw)
        if self.type in _LIST_LIKE:
            return [int(v) for v in raw.split(",") if v]
        return raw

    def serialize(self, value: SettingValue) -> Optional[str]:
        """Serialize a Python value to the string form stored in the database.
        :param value: the value to store
        :return: its string representation, or None to store SQL NULL
        """
        if value is None:
            return None
        if self.type is SettingType.BOOLEAN:
            return "true" if value else "false"
        if self.type in _LIST_LIKE:
            return ",".join(str(v) for v in value or [])
        return str(value)


class SettingsRegistry:
    """Central store of every cog-registered setting, backed by the bot_settings table."""

    def __init__(self, db: Database):
        """
        :param db: core Database wrapper
        """
        self._db = db
        self._definitions: dict[str, SettingDefinition] = {}
        self._cache: dict[str, SettingValue] = {}

    def register(self, definition: SettingDefinition) -> None:
        """Register a setting so it is persisted and shown in the settings UI.
        :param definition: the setting's key, type, default, and display info
        """
        if definition.key in self._definitions:
            raise ValueError(f"setting key already registered: {definition.key}")
        if definition.type is SettingType.CHOICE and not definition.choices:
            raise ValueError(
                f"setting {definition.key!r} is a CHOICE type but has no choices"
            )
        self._definitions[definition.key] = definition

    async def load(self) -> None:
        """Populate the in-memory cache from the database, filling in defaults for unset keys."""
        rows = await self._db.fetch("SELECT key, value FROM bot_settings")
        stored = {row["key"]: row["value"] for row in rows}
        for key, definition in self._definitions.items():
            raw = stored.get(key)
            self._cache[key] = (
                definition.cast(raw) if raw is not None else definition.default
            )

    def get(self, key: str) -> SettingValue:
        """Return a setting's current value.
        :param key: the setting's registered key
        :return: the setting's current value, or its default if never set
        """
        return self._cache[key]

    async def set(self, key: str, value: SettingValue) -> None:
        """Validate, persist, and cache a new value for a setting.
        :param key: the setting's registered key
        :param value: the new value
        """
        definition = self._definitions[key]
        if definition.type is SettingType.CHOICE and value not in (
            definition.choices or []
        ):
            raise ValueError(
                f"{value!r} is not one of {definition.choices} for setting {key!r}"
            )

        raw = definition.serialize(value)
        await self._db.execute(
            """
            INSERT INTO bot_settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            key,
            raw,
        )
        self._cache[key] = value

    def categories(self) -> list[str]:
        """List the distinct categories settings are grouped under.
        :return: sorted category names
        """
        return sorted({d.category for d in self._definitions.values()})

    def by_category(self, category: str) -> list[SettingDefinition]:
        """List the settings registered under one category.
        :param category: category name
        :return: matching setting definitions, in registration order
        """
        return [d for d in self._definitions.values() if d.category == category]

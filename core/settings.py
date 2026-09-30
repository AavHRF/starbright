from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Union

from core.database import Database

if TYPE_CHECKING:
    import discord

SettingValue = Union[bool, int, float, str, list[int], None]

# Renders a preview of what a category's settings produce: given the member viewing it and a getter that
# returns each setting's value (with any pending, unsaved change applied), return the items to display.
PreviewBuilder = Callable[
    ["discord.Member", Callable[[str], SettingValue]], list["discord.ui.Item"]
]

# Builds a custom settings screen for a category whose settings don't fit the one-value-per-key model,
# given the interaction that opened it.
PageFactory = Callable[["discord.Interaction"], "discord.ui.LayoutView"]


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
    # Whether the setting can be cleared back to None (an empty modal submission, or an empty picker).
    optional: bool = False
    # Whether a STRING setting is edited with a multi-line text box.
    multiline: bool = False
    # Checks and cleans up an entered value, raising ValueError with a user-facing message if it's invalid.
    normalize: Optional[Callable[[SettingValue], SettingValue]] = None

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
        self._previews: dict[str, PreviewBuilder] = {}
        self._pages: dict[str, PageFactory] = {}

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

    def register_preview(self, category: str, builder: PreviewBuilder) -> None:
        """Add a Preview button to a category's settings screen, showing what its settings produce.
        :param category: the category to add the preview to
        :param builder: renders the preview for a given set of values
        """
        self._previews[category] = builder

    def preview_for(self, category: str) -> Optional[PreviewBuilder]:
        """Return the preview builder registered for a category, if any.
        :param category: category name
        :return: the category's preview builder, or None if it has no preview
        """
        return self._previews.get(category)

    def register_page(self, category: str, factory: PageFactory) -> None:
        """List a custom screen in the settings menu, for a category the standard screen can't represent.
        :param category: the category name to list it under
        :param factory: builds the screen when it's opened
        """
        if category in self._pages:
            raise ValueError(f"settings page already registered: {category}")
        self._pages[category] = factory

    def page_for(self, category: str) -> Optional[PageFactory]:
        """Return the custom screen registered for a category, if any.
        :param category: category name
        :return: the category's page factory, or None if it uses the standard screen
        """
        return self._pages.get(category)

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
        return sorted({d.category for d in self._definitions.values()} | set(self._pages))

    def by_category(self, category: str) -> list[SettingDefinition]:
        """List the settings registered under one category.
        :param category: category name
        :return: matching setting definitions, in registration order
        """
        return [d for d in self._definitions.values() if d.category == category]

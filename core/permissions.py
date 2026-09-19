from __future__ import annotations

import enum
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from core.settings import SettingDefinition, SettingsRegistry, SettingType

if TYPE_CHECKING:
    from core.bot import StarbrightBot


class PermissionTier(enum.IntEnum):
    """Ordered bot permission levels; each tier includes everything granted by the ones below it."""

    USER = 0
    SUPERUSER = 1
    MODERATOR = 2
    ADMINISTRATOR = 3


class InsufficientTier(app_commands.CheckFailure):
    """Raised when a member's permission tier is below what a command requires."""

    def __init__(self, required: PermissionTier):
        """
        :param required: the minimum tier that was required
        """
        self.required = required
        super().__init__(f"requires {required.name} or higher")


_RANKED_TIERS = (
    PermissionTier.ADMINISTRATOR,
    PermissionTier.MODERATOR,
    PermissionTier.SUPERUSER,
)
_ROLE_KEYS = {tier: f"permissions.{tier.name.lower()}_roles" for tier in _RANKED_TIERS}
_USER_KEYS = {tier: f"permissions.{tier.name.lower()}_users" for tier in _RANKED_TIERS}


class PermissionRegistry:
    """Resolves a member's permission tier from role/user assignments stored as bot settings."""

    def __init__(self, settings: SettingsRegistry):
        """
        :param settings: the core settings registry to store tier assignments in
        """
        self._settings = settings
        for tier in _RANKED_TIERS:
            settings.register(
                SettingDefinition(
                    key=_ROLE_KEYS[tier],
                    category="Permissions",
                    label=f"{tier.name.title()} roles",
                    description=f"Roles granted the {tier.name.title()} permission tier.",
                    type=SettingType.MULTI_ROLE,
                    default=[],
                    min_tier=PermissionTier.ADMINISTRATOR,
                )
            )
            settings.register(
                SettingDefinition(
                    key=_USER_KEYS[tier],
                    category="Permissions",
                    label=f"{tier.name.title()} users",
                    description=f"Individual users granted the {tier.name.title()} permission tier.",
                    type=SettingType.MULTI_USER,
                    default=[],
                    min_tier=PermissionTier.ADMINISTRATOR,
                )
            )

    def get_tier(self, member: discord.abc.User) -> PermissionTier:
        """Resolve a member's effective permission tier.
        :param member: the user or member to resolve
        :return: the highest tier that applies, defaulting to USER
        """
        if (
            isinstance(member, discord.Member)
            and member.guild_permissions.administrator
        ):
            return PermissionTier.ADMINISTRATOR

        role_ids = {role.id for role in getattr(member, "roles", [])}
        for tier in _RANKED_TIERS:
            if member.id in self._settings.get(_USER_KEYS[tier]):
                return tier
            if role_ids.intersection(self._settings.get(_ROLE_KEYS[tier])):
                return tier
        return PermissionTier.USER

    def has_tier(self, member: discord.abc.User, required: PermissionTier) -> bool:
        """Check whether a member meets a minimum permission tier.
        :param member: the user or member to check
        :param required: the minimum tier required
        :return: True if the member's tier is at least the required one
        """
        return self.get_tier(member) >= required


def require_tier(tier: PermissionTier):
    """Build an app_commands check that rejects members below the given permission tier.
    :param tier: the minimum tier required to use the command
    :return: a check decorator usable with @app_commands.check
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        bot: StarbrightBot = interaction.client  # type: ignore[assignment]
        if not bot.permissions.has_tier(interaction.user, tier):
            raise InsufficientTier(tier)
        return True

    return app_commands.check(predicate)

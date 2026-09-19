from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

import discord

from core.permissions import PermissionTier

ParameterKind = Literal["user", "channel", "role", "string", "integer", "boolean"]


@dataclass
class ActionContext:
    """Execution context for a chain-invoked action: live interaction vs. a background firing."""

    bot: Any
    guild: discord.Guild
    channel: discord.abc.Messageable
    user: discord.abc.User
    interaction: Optional[discord.Interaction] = None

    async def reply(self, content: str, **kwargs: Any) -> None:
        """Send a reply through the interaction if one is live, or straight to the channel otherwise.
        :param content: the message to send
        :param kwargs: forwarded to send_message/followup.send/channel.send
        """
        if self.interaction is not None:
            if self.interaction.response.is_done():
                await self.interaction.followup.send(content, **kwargs)
            else:
                await self.interaction.response.send_message(content, **kwargs)
        else:
            kwargs.pop("ephemeral", None)
            await self.channel.send(content, **kwargs)


@dataclass
class ActionParameter:
    """Describes one parameter an action accepts, for coercion when invoked from a raw chain string."""

    name: str
    kind: ParameterKind
    required: bool = True
    default: Any = None


@dataclass
class ActionSpec:
    """A chainable unit of command logic: its parameters, inherent tier requirement, and handler."""

    name: str
    description: str
    parameters: list[ActionParameter]
    min_tier: PermissionTier
    handler: Callable[..., Awaitable[None]]


class ActionRegistry:
    """Central store of every action a cog has made available to combine/alias/schedule/repeat/trigger."""

    def __init__(self):
        self._actions: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        """Register an action so meta-commands can invoke it by name.
        :param spec: the action's name, parameters, tier requirement, and handler
        """
        if spec.name in self._actions:
            raise ValueError(f"action already registered: {spec.name}")
        self._actions[spec.name] = spec

    def get(self, name: str) -> Optional[ActionSpec]:
        """Look up a registered action by name.
        :param name: the action's registered name
        :return: the matching ActionSpec, or None
        """
        return self._actions.get(name)

    def names(self) -> list[str]:
        """List every registered action name.
        :return: sorted action names
        """
        return sorted(self._actions)

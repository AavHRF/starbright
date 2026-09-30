import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.actions import ActionRegistry
from core.api.client import NSApiClient
from core.config import Config
from core.database import Database
from core.permissions import InsufficientTier, PermissionRegistry
from core.settings import SettingsRegistry
from core.sse import SseFeed

logger = logging.getLogger(__name__)

COGS_DIR = Path(__file__).resolve().parent.parent / "cogs"


class StarbrightBot(commands.Bot):

    def __init__(self, config: Config):
        """
        :param config: loaded bot configuration
        """
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=config.command_prefix, intents=intents)

        self.config = config
        self.db = Database(config.postgres_dsn)
        self.api = NSApiClient(config.ns_user_agent)
        self.settings = SettingsRegistry(self.db)
        self.permissions = PermissionRegistry(self.settings)
        self.actions = ActionRegistry()
        self.sse = SseFeed(self.db, config.ns_user_agent, self.settings)
        self.tree.on_error = self._on_app_command_error

    async def setup_hook(self) -> None:
        """Connect core services, load cogs, then hydrate settings and sync slash commands."""
        await self.db.connect()
        await self.api.start()
        await self._load_cogs()
        await self.settings.load()
        # Started last so every cog has subscribed before the first event is delivered.
        await self.sse.start()
        await self.tree.sync()

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Reply with a friendly ephemeral message on a permission check failure, or log anything else.
        :param interaction: the interaction that failed
        :param error: the error raised while checking or invoking the command
        """
        if isinstance(error, InsufficientTier):
            await interaction.response.send_message(
                f"You need {error.required.name.title()} permissions or higher to use this.",
                ephemeral=True,
            )
            return
        logger.exception("Unhandled app command error", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong running that command.", ephemeral=True
            )

    async def _load_cogs(self) -> None:
        """Load every cog module found in the cogs/ package."""
        for path in sorted(COGS_DIR.glob("*.py")):
            if path.stem == "__init__":
                continue
            await self.load_extension(f"cogs.{path.stem}")
            logger.info("Loaded cog: %s", path.stem)

    async def close(self) -> None:
        await self.sse.stop()
        await super().close()
        await self.db.close()
        await self.api.close()

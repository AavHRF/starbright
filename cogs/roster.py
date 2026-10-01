import discord
from discord import app_commands
from discord.ext import commands

from core.bot import StarbrightBot
from core.models.starborn import Starborn, StarbornStatus
from core.permissions import PermissionTier
from core.settings import SettingDefinition, SettingType

_ROLE_KEY = "roster.role"
_CATEGORY = "Roster"

class Roster(commands.Cog):
    """/roster [add / fetch / amend / delete / list]: manually edit the roster."""

    def __init__(self, bot: StarbrightBot):
        """
        :param bot: the running bot instance
        """
        self.bot = bot
        bot.settings.register(
            SettingDefinition(
                key=_ROLE_KEY,
                category=_CATEGORY,
                label="Roster role",
                description="The role allowed to use /roster commands. Moderators and administrators always can.",
                type=SettingType.ROLE,
                min_tier=PermissionTier.ADMINISTRATOR,
                optional=True,
            )
        )

    def _can_roster(self, member: discord.abc.User) -> bool:
        """Check whether a member may use /roster commands.
        :param member: the member running the command
        :return: True if they hold the configured role or are at least a moderator
        """
        if self.bot.permissions.has_tier(member, PermissionTier.MODERATOR):
            return True
        role_id = self.bot.settings.get(_ROLE_KEY)
        return bool(role_id) and any(
            role.id == role_id for role in getattr(member, "roles", [])
        )

    roster_group = discord.app_commands.Group(name='roster', description='Manual control of the member roster')

    @roster_group.command(
        name="add",
        description="Add a member to the roster.",
    )
    @app_commands.guild_only()
    async def add(self, interaction: discord.Interaction, discord_account: discord.Member, stl_nation: str | None, hzn_nation: str | None, status: StarbornStatus):
        if not self._can_roster(interaction.user):
            await interaction.response.send_message(
                "You aren't allowed to use /roster commands.", ephemeral=True
            )
            return
        await Starborn.create(self.bot.db, discord_account.id, stl_nation, hzn_nation, status)
        await interaction.response.send_message(
            "Member successfully created.", ephemeral=True
        )

    @roster_group.command(
        name="fetch",
        description="Fetch a member from the roster by internal ID, nation, or Discord account.",
    )
    @app_commands.guild_only()
    async def fetch(self, interaction: discord.Interaction, id: int | None, discord_account: discord.Member | None, stl_nation: str | None, hzn_nation: str | None):
        if not self._can_roster(interaction.user):
            await interaction.response.send_message(
                "You aren't allowed to use /roster commands.", ephemeral=True
            )
            return

        if id is not None:
            record = await Starborn.get_by_id(self.bot.db, id)
        elif discord_account is not None:
            record = await Starborn.get_by_discord_id(self.bot.db, discord_account.id)
        elif stl_nation is not None:
            record = await Starborn.get_by_stl_nation(self.bot.db, stl_nation)
        elif hzn_nation is not None:
            record = await Starborn.get_by_hzn_nation(self.bot.db, hzn_nation)
        else: # we must have run out of things to check by
            await interaction.response.send_message(
                "You must provide at least one argument (`discord_account`, `stl_nation` or `hzn_nation`).", ephemeral=True
            )
            return

        if record is None:
            await interaction.response.send_message(
                "No member was found matching the specified criteria.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title = "__**Member Record**__",
            description = f"""**ID:** {record.st_id}
            **Discord account:** <@{record.discord_id}>
            **Starlight nation:** {record.stl_nation}
            **Horizon nation:** {record.hzn_nation}
            **Status:** {str(record.status).title()}
            """
        )

        await interaction.response.send_message(
            embed = embed
        )

    @roster_group.command(
        name="amend",
        description="Amend a member on the roster. Nation fields must be provided or they will be blanked.",
    )
    @app_commands.guild_only()
    async def amend(self, interaction: discord.Interaction, id: int, discord_account: discord.Member | None, stl_nation: str | None, hzn_nation: str | None, status: StarbornStatus | None):
        if not self._can_roster(interaction.user):
            await interaction.response.send_message(
                "You aren't allowed to use /roster commands.", ephemeral=True
            )
            return
        member = await Starborn.get_by_id(self.bot.db, id)

        if discord_account is not None:
            member.discord_id = discord_account.id
        if status is not None:
            member.status = status
        member.stl_nation = stl_nation
        member.hzn_nation = hzn_nation

        await member.save(self.bot.db)

        embed = discord.Embed(
            title = "__**Amended Member Record**__",
            description = f"""**ID:** {record.st_id}
            **Discord account:** <@{record.discord_id}>
            **Starlight nation:** {record.stl_nation}
            **Horizon nation:** {record.hzn_nation}
            **Status:** {str(record.status).title()}
            """
        )

        await interaction.response.send_message(
            "Member successfully amended.", embed=embed, ephemeral=True
        )

async def setup(bot: StarbrightBot) -> None:
    """Entry point discord.py calls to load this cog.
    :param bot: the running bot instance
    """
    await bot.add_cog(Roster(bot))

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.actions import ActionContext
from core.bot import StarbrightBot
from core.chains import (
    execute_chain,
    find_placeholders,
    inherited_min_tier,
    parse_chain,
    parse_duration,
    substitute_tokens,
    validate_chain,
)
from core.permissions import PermissionTier, require_tier

logger = logging.getLogger(__name__)

_TRIGGER_EVENTS = ["member_join", "member_remove", "message"]

# Placeholder token -> the /run argument that binds it.
_RUN_BINDINGS = {
    "$u1": "user1",
    "$u2": "user2",
    "$c1": "channel1",
    "$r1": "role1",
    "$s1": "string1",
    "$s2": "string2",
}

# Placeholders a trigger chain may use, bound from the event when it fires.
_TRIGGER_PLACEHOLDERS = {
    "member_join": {"$u1"},
    "member_remove": {"$u1"},
    "message": {"$u1", "$c1", "$s1"},
}

# Recurring jobs can't fire more often than this; the poller itself only runs every 15 seconds.
MIN_REPEAT_SECONDS = 60


@dataclass
class _StaticUser:
    """Minimal stand-in for a Discord user when a scheduled/triggered job's original member has left."""

    id: int

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


def _utc_now() -> datetime:
    """Return the current time as a naive UTC datetime.
    :return: the current UTC time, tzinfo stripped
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _discord_timestamp(when: datetime) -> str:
    """Render a stored naive-UTC datetime as a Discord relative timestamp tag.
    :param when: a naive UTC datetime, as stored in the database
    :return: a '<t:...:R>' tag Discord renders client-side as a relative time
    """
    return f"<t:{int(when.replace(tzinfo=timezone.utc).timestamp())}:R>"


class MetaCommands(commands.Cog):
    """combine/confirm/approve/alias/run/schedule/repeat/trigger: commands that modify other commands' execution."""

    alias_group = app_commands.Group(
        name="alias", description="Manage saved command-chain aliases.", guild_only=True
    )
    schedule_group = app_commands.Group(
        name="schedule",
        description="Run a command chain once, after a delay.",
        guild_only=True,
    )
    repeat_group = app_commands.Group(
        name="repeat",
        description="Run a command chain on a recurring interval.",
        guild_only=True,
    )
    trigger_group = app_commands.Group(
        name="trigger",
        description="Run a command chain when an event happens.",
        guild_only=True,
    )

    def __init__(self, bot: StarbrightBot):
        """
        :param bot: the running bot instance
        """
        self.bot = bot
        self._running_jobs: dict[int, asyncio.Task[None]] = {}
        # source channel ID -> the message triggers watching it. Each list is replaced rather than mutated,
        # so an on_message call already iterating one is unaffected by a trigger being added or removed.
        self._message_triggers: dict[int, list[asyncpg.Record]] = {}

    async def cog_load(self) -> None:
        """Cache the message triggers and start the background job poller once the cog is added."""
        rows = await self.bot.db.fetch(
            "SELECT * FROM meta_triggers WHERE event = 'message'"
        )
        for row in rows:
            self._message_triggers.setdefault(row["source_channel_id"], []).append(row)
        self._poll_jobs.start()

    async def cog_unload(self) -> None:
        """Stop the background job poller and any chains it is still running when the cog is removed."""
        self._poll_jobs.cancel()
        for task in self._running_jobs.values():
            task.cancel()

    def _actor(self, guild: discord.Guild, user_id: int) -> discord.abc.User:
        """Resolve the member a stored job/trigger should run as, falling back to a stand-in if they've left.
        :param guild: the guild the chain runs in
        :param user_id: the Discord ID of the user who created the job or trigger
        :return: the live member, or a minimal stand-in carrying only their ID
        """
        return guild.get_member(user_id) or _StaticUser(user_id)

    @staticmethod
    def _context_from_interaction(interaction: discord.Interaction) -> ActionContext:
        """Build a chain ActionContext from a live interaction.
        :param interaction: the invoking interaction
        :return: the equivalent ActionContext
        """
        return ActionContext(
            bot=interaction.client,
            guild=interaction.guild,
            channel=interaction.channel,
            user=interaction.user,
            interaction=interaction,
        )

    # --- combine ---------------------------------------------------------------------------

    @app_commands.command(
        name="combine", description="Run a chain of commands in sequence."
    )
    @app_commands.guild_only()
    async def combine(self, interaction: discord.Interaction, chain: str) -> None:
        """Parse and immediately run a literal command chain.
        :param interaction: the command invocation interaction
        :param chain: the pipe-delimited chain string, e.g. 'confirm | kick member:<@123> reason:"spam"'
        """
        try:
            steps = parse_chain(chain)
            validate_chain(self.bot, steps)
        except ValueError as exc:
            await interaction.response.send_message(
                f"Invalid chain: {exc}", ephemeral=True
            )
            return
        await execute_chain(self._context_from_interaction(interaction), steps)

    # --- alias / run -------------------------------------------------------------------------

    @alias_group.command(name="create")
    @app_commands.choices(
        min_tier=[
            app_commands.Choice(name=t.name.title(), value=t.name)
            for t in PermissionTier
        ]
    )
    @require_tier(PermissionTier.ADMINISTRATOR)
    async def alias_create(
        self,
        interaction: discord.Interaction,
        name: str,
        chain: str,
        min_tier: Optional[str] = None,
    ) -> None:
        """Save a chain template as a reusable alias, optionally overriding its required permission tier.
        :param interaction: the command invocation interaction
        :param name: the alias's name, used later with /run
        :param chain: the chain template, e.g. 'confirm kick member:$u1 reason:$s1'
        :param min_tier: if set, overrides the tier inherited from the chain's own commands
        """
        try:
            steps = parse_chain(chain)
            validate_chain(self.bot, steps, allowed_placeholders=_RUN_BINDINGS)
        except ValueError as exc:
            await interaction.response.send_message(
                f"Invalid chain: {exc}", ephemeral=True
            )
            return

        result = await self.bot.db.execute(
            "INSERT INTO meta_aliases (name, chain, min_tier, created_by) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (name) DO NOTHING",
            name,
            chain,
            PermissionTier[min_tier].value if min_tier else None,
            interaction.user.id,
        )
        if result == "INSERT 0 0":
            await interaction.response.send_message(
                f"An alias named `{name}` already exists.", ephemeral=True
            )
            return

        placeholders = ", ".join(sorted(find_placeholders(steps))) or "none"
        await interaction.response.send_message(
            f"Created alias `{name}`. Placeholders used: {placeholders}.",
            ephemeral=True,
        )

    @alias_group.command(name="delete")
    @require_tier(PermissionTier.ADMINISTRATOR)
    async def alias_delete(self, interaction: discord.Interaction, name: str) -> None:
        """Delete a saved alias.
        :param interaction: the command invocation interaction
        :param name: the alias to delete
        """
        result = await self.bot.db.execute(
            "DELETE FROM meta_aliases WHERE name = $1", name
        )
        if result == "DELETE 0":
            await interaction.response.send_message(
                f"No alias named `{name}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Deleted alias `{name}`.", ephemeral=True
            )

    @alias_group.command(name="list")
    async def alias_list(self, interaction: discord.Interaction) -> None:
        """List every saved alias and its chain.
        :param interaction: the command invocation interaction
        """
        rows = await self.bot.db.fetch(
            "SELECT name, chain FROM meta_aliases ORDER BY name"
        )
        if not rows:
            await interaction.response.send_message(
                "No aliases have been created yet.", ephemeral=True
            )
            return
        lines = [f"**{row['name']}** → `{row['chain']}`" for row in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _alias_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest saved alias names matching what's been typed so far.
        :param interaction: the in-progress command invocation
        :param current: the partial text typed into the alias field
        :return: up to 25 matching alias-name choices
        """
        rows = await self.bot.db.fetch(
            "SELECT name FROM meta_aliases WHERE name ILIKE $1 ORDER BY name LIMIT 25",
            f"%{current}%",
        )
        return [
            app_commands.Choice(name=row["name"], value=row["name"]) for row in rows
        ]

    @app_commands.command(name="run", description="Run a saved alias.")
    @app_commands.guild_only()
    @app_commands.autocomplete(alias=_alias_autocomplete)
    async def run(
        self,
        interaction: discord.Interaction,
        alias: str,
        user1: Optional[discord.Member] = None,
        user2: Optional[discord.Member] = None,
        channel1: Optional[discord.abc.GuildChannel] = None,
        role1: Optional[discord.Role] = None,
        string1: Optional[str] = None,
        string2: Optional[str] = None,
    ) -> None:
        """Run a saved alias, binding any of its $u1/$u2/$c1/$r1/$s1/$s2 placeholders to the given arguments.
        :param interaction: the command invocation interaction
        :param alias: the alias's name
        :param user1: bound to $u1
        :param user2: bound to $u2
        :param channel1: bound to $c1
        :param role1: bound to $r1
        :param string1: bound to $s1
        :param string2: bound to $s2
        """
        row = await self.bot.db.fetchrow(
            "SELECT chain, min_tier FROM meta_aliases WHERE name = $1", alias
        )
        if row is None:
            await interaction.response.send_message(
                f"No alias named `{alias}`.", ephemeral=True
            )
            return

        steps = parse_chain(row["chain"])
        given = {
            "user1": user1,
            "user2": user2,
            "channel1": channel1,
            "role1": role1,
            "string1": string1,
            "string2": string2,
        }
        bindings = {
            token: given[arg]
            for token, arg in _RUN_BINDINGS.items()
            if given[arg] is not None
        }
        steps = substitute_tokens(steps, bindings)

        unbound = find_placeholders(steps)
        if unbound:
            needed = ", ".join(
                f"`{_RUN_BINDINGS.get(token, token)}`" for token in sorted(unbound)
            )
            await interaction.response.send_message(
                f"Alias `{alias}` needs the {needed} argument(s).", ephemeral=True
            )
            return

        overall_tier = (
            PermissionTier(row["min_tier"])
            if row["min_tier"] is not None
            else inherited_min_tier(self.bot, steps)
        )
        ctx = self._context_from_interaction(interaction)
        await execute_chain(ctx, steps, overall_min_tier=overall_tier)

    # --- schedule / repeat -------------------------------------------------------------------

    async def _create_job(
        self,
        interaction: discord.Interaction,
        chain: str,
        duration_text: str,
        *,
        recurring: bool,
    ) -> None:
        """Validate a chain and duration, then persist it as a one-off or recurring job.
        :param interaction: the command invocation interaction
        :param chain: the chain to run when the job fires
        :param duration_text: a duration string like '10m', '2h', or '1d'
        :param recurring: whether this job repeats on that interval, or fires once
        """
        try:
            steps = parse_chain(chain)
            validate_chain(self.bot, steps)
            delta = parse_duration(duration_text)
            if recurring and delta.total_seconds() < MIN_REPEAT_SECONDS:
                raise ValueError(
                    f"recurring interval must be at least {MIN_REPEAT_SECONDS}s"
                )
        except ValueError as exc:
            await interaction.response.send_message(
                f"Invalid input: {exc}", ephemeral=True
            )
            return

        next_run = _utc_now() + delta
        interval_seconds = int(delta.total_seconds()) if recurring else None
        row = await self.bot.db.fetchrow(
            "INSERT INTO meta_jobs (chain, guild_id, channel_id, user_id, next_run, interval_seconds) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            chain,
            interaction.guild.id,
            interaction.channel.id,
            interaction.user.id,
            next_run,
            interval_seconds,
        )
        kind = "Recurring job" if recurring else "Scheduled run"
        await interaction.response.send_message(
            f"{kind} `#{row['id']}` created, next at {_discord_timestamp(next_run)}.",
            ephemeral=True,
        )

    async def _list_jobs(
        self, interaction: discord.Interaction, *, recurring: bool
    ) -> None:
        """List either the one-off scheduled jobs or the recurring ones.
        :param interaction: the command invocation interaction
        :param recurring: True to list recurring jobs, False for one-off ones
        """
        rows = await self.bot.db.fetch(
            "SELECT id, chain, next_run FROM meta_jobs WHERE (interval_seconds IS NOT NULL) = $1 ORDER BY id",
            recurring,
        )
        if not rows:
            await interaction.response.send_message(
                "Nothing scheduled.", ephemeral=True
            )
            return
        lines = [
            f"`#{r['id']}` {_discord_timestamp(r['next_run'])} → `{r['chain']}`"
            for r in rows
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _cancel_job(self, interaction: discord.Interaction, job_id: int) -> None:
        """Delete a scheduled or recurring job by ID.
        :param interaction: the command invocation interaction
        :param job_id: the job's ID, as shown by /schedule list or /repeat list
        """
        result = await self.bot.db.execute(
            "DELETE FROM meta_jobs WHERE id = $1", job_id
        )
        if result == "DELETE 0":
            await interaction.response.send_message(
                f"No job with ID {job_id}.", ephemeral=True
            )
        else:
            await interaction.response.send_message("Cancelled.", ephemeral=True)

    @schedule_group.command(name="create")
    @require_tier(PermissionTier.SUPERUSER)
    async def schedule_create(
        self, interaction: discord.Interaction, chain: str, delay: str
    ) -> None:
        """Run a chain once, after a delay.
        :param interaction: the command invocation interaction
        :param chain: the chain to run
        :param delay: how long to wait, e.g. '10m', '2h', '1d'
        """
        await self._create_job(interaction, chain, delay, recurring=False)

    @schedule_group.command(name="list")
    async def schedule_list(self, interaction: discord.Interaction) -> None:
        """List pending one-off scheduled runs.
        :param interaction: the command invocation interaction
        """
        await self._list_jobs(interaction, recurring=False)

    @schedule_group.command(name="cancel")
    @require_tier(PermissionTier.SUPERUSER)
    async def schedule_cancel(self, interaction: discord.Interaction, id: int) -> None:
        """Cancel a pending scheduled run.
        :param interaction: the command invocation interaction
        :param id: the job's ID, as shown by /schedule list
        """
        await self._cancel_job(interaction, id)

    @repeat_group.command(name="create")
    @require_tier(PermissionTier.SUPERUSER)
    async def repeat_create(
        self, interaction: discord.Interaction, chain: str, every: str
    ) -> None:
        """Run a chain on a recurring interval.
        :param interaction: the command invocation interaction
        :param chain: the chain to run
        :param every: the interval, e.g. '10m', '2h', '1d'
        """
        await self._create_job(interaction, chain, every, recurring=True)

    @repeat_group.command(name="list")
    async def repeat_list(self, interaction: discord.Interaction) -> None:
        """List active recurring jobs.
        :param interaction: the command invocation interaction
        """
        await self._list_jobs(interaction, recurring=True)

    @repeat_group.command(name="cancel")
    @require_tier(PermissionTier.SUPERUSER)
    async def repeat_cancel(self, interaction: discord.Interaction, id: int) -> None:
        """Stop a recurring job.
        :param interaction: the command invocation interaction
        :param id: the job's ID, as shown by /repeat list
        """
        await self._cancel_job(interaction, id)

    @tasks.loop(seconds=15)
    async def _poll_jobs(self) -> None:
        """Fire every scheduled/recurring job whose next_run has arrived, rescheduling or deleting it after.

        Each due job runs as its own task so a slow chain (e.g. one waiting on an approve gate) can't
        block other due jobs; a job still running from a previous poll is skipped rather than re-fired.
        """
        try:
            due = await self.bot.db.fetch(
                "SELECT * FROM meta_jobs WHERE next_run <= $1", _utc_now()
            )
        except Exception:
            logger.exception("Could not fetch due jobs; will retry next poll")
            return

        for row in due:
            if row["id"] in self._running_jobs:
                continue
            task = asyncio.create_task(self._fire_job(row))
            self._running_jobs[row["id"]] = task
            task.add_done_callback(
                lambda _, job_id=row["id"]: self._running_jobs.pop(job_id, None)
            )

    @_poll_jobs.before_loop
    async def _before_poll_jobs(self) -> None:
        """Wait for the gateway connection and channel cache before firing anything, so jobs aren't
        mistaken for orphaned (and deleted) just because the bot hasn't finished starting up.
        """
        await self.bot.wait_until_ready()
        # An error escaping before_loop would stop the poller for good, so the backfill must not raise.
        try:
            await self._backfill_guild_ids()
        except Exception:
            logger.exception("Could not backfill guild IDs; will retry on next startup")

    async def _backfill_guild_ids(self) -> None:
        """Fill in guild_id for jobs/triggers created before that column existed, resolved from their channel."""
        for table in ("meta_jobs", "meta_triggers"):
            rows = await self.bot.db.fetch(
                f"SELECT id, channel_id FROM {table} WHERE guild_id IS NULL"
            )
            for row in rows:
                channel = self.bot.get_channel(row["channel_id"])
                guild = getattr(channel, "guild", None)
                if guild is None:
                    continue
                await self.bot.db.execute(
                    f"UPDATE {table} SET guild_id = $1 WHERE id = $2",
                    guild.id,
                    row["id"],
                )

    async def _fire_job(self, row) -> None:
        """Run one due job's chain, then reschedule it (recurring) or delete it (one-off).

        Every failure is caught and logged so one bad job or a transient database error can't stop the loop.
        :param row: the due row from meta_jobs
        """
        try:
            channel = self.bot.get_channel(row["channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(row["channel_id"])
                except discord.NotFound:
                    channel = None
                except discord.HTTPException:
                    logger.exception(
                        "Could not fetch channel %s for job %s; will retry next poll",
                        row["channel_id"],
                        row["id"],
                    )
                    return

            guild = getattr(channel, "guild", None)
            if channel is None or guild is None:
                logger.warning(
                    "Deleting job %s: its channel %s is gone or not a guild channel",
                    row["id"],
                    row["channel_id"],
                )
                await self.bot.db.execute(
                    "DELETE FROM meta_jobs WHERE id = $1", row["id"]
                )
                return

            user = self._actor(guild, row["user_id"])
            ctx = ActionContext(
                bot=self.bot, guild=guild, channel=channel, user=user, interaction=None
            )
            try:
                await execute_chain(ctx, parse_chain(row["chain"]))
            except Exception:
                logger.exception("Error firing job %s", row["id"])

            if row["interval_seconds"]:
                interval = timedelta(seconds=row["interval_seconds"])
                next_run = row["next_run"] + interval
                now = _utc_now()
                while next_run <= now:
                    next_run += interval
                await self.bot.db.execute(
                    "UPDATE meta_jobs SET next_run = $1 WHERE id = $2",
                    next_run,
                    row["id"],
                )
            else:
                await self.bot.db.execute(
                    "DELETE FROM meta_jobs WHERE id = $1", row["id"]
                )
        except Exception:
            logger.exception("Error processing job %s", row["id"])

    # --- trigger -------------------------------------------------------------------------------

    @trigger_group.command(name="create")
    @app_commands.choices(
        event=[
            app_commands.Choice(name=e.replace("_", " ").title(), value=e)
            for e in _TRIGGER_EVENTS
        ]
    )
    @require_tier(PermissionTier.SUPERUSER)
    async def trigger_create(
        self,
        interaction: discord.Interaction,
        event: str,
        chain: str,
        source_channel: Optional[discord.abc.GuildChannel] = None,
        contains: Optional[str] = None,
    ) -> None:
        """Run a chain automatically whenever a given event happens.
        :param interaction: the command invocation interaction
        :param event: the event to trigger on
        :param chain: the chain to run when it fires
        :param source_channel: for 'message' triggers, the channel to watch (required for that event)
        :param contains: for 'message' triggers, only fire if the message contains this text (optional)
        """
        if event == "message":
            if source_channel is None:
                await interaction.response.send_message(
                    "`message` triggers require a `source_channel` to watch.",
                    ephemeral=True,
                )
                return
        elif source_channel is not None or contains is not None:
            await interaction.response.send_message(
                "`source_channel`/`contains` only apply to `message` triggers.",
                ephemeral=True,
            )
            return

        try:
            steps = parse_chain(chain)
            validate_chain(self.bot, steps)
        except ValueError as exc:
            await interaction.response.send_message(
                f"Invalid chain: {exc}", ephemeral=True
            )
            return

        row = await self.bot.db.fetchrow(
            "INSERT INTO meta_triggers (event, chain, guild_id, channel_id, created_by, source_channel_id, contains) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
            event,
            chain,
            interaction.guild.id,
            interaction.channel.id,
            interaction.user.id,
            source_channel.id if source_channel else None,
            contains,
        )
        if event == "message":
            watching = self._message_triggers.get(source_channel.id, [])
            self._message_triggers[source_channel.id] = [*watching, row]
        await interaction.response.send_message(
            f"Trigger `#{row['id']}` created for `{event}`.", ephemeral=True
        )

    @trigger_group.command(name="list")
    async def trigger_list(self, interaction: discord.Interaction) -> None:
        """List every active trigger.
        :param interaction: the command invocation interaction
        """
        rows = await self.bot.db.fetch(
            "SELECT id, event, chain, source_channel_id, contains FROM meta_triggers ORDER BY id"
        )
        if not rows:
            await interaction.response.send_message(
                "No triggers have been created yet.", ephemeral=True
            )
            return
        lines = []
        for r in rows:
            filt = ""
            if r["event"] == "message":
                filt = f" in <#{r['source_channel_id']}>"
                if r["contains"]:
                    filt += f" containing \"{r['contains']}\""
            lines.append(f"`#{r['id']}` **{r['event']}**{filt} → `{r['chain']}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @trigger_group.command(name="remove")
    @require_tier(PermissionTier.SUPERUSER)
    async def trigger_remove(self, interaction: discord.Interaction, id: int) -> None:
        """Remove a trigger.
        :param interaction: the command invocation interaction
        :param id: the trigger's ID, as shown by /trigger list
        """
        row = await self.bot.db.fetchrow(
            "DELETE FROM meta_triggers WHERE id = $1 RETURNING event, source_channel_id",
            id,
        )
        if row is None:
            await interaction.response.send_message(
                f"No trigger with ID {id}.", ephemeral=True
            )
            return

        if row["event"] == "message":
            channel_id = row["source_channel_id"]
            remaining = [
                trigger
                for trigger in self._message_triggers.get(channel_id, [])
                if trigger["id"] != id
            ]
            if remaining:
                self._message_triggers[channel_id] = remaining
            else:
                self._message_triggers.pop(channel_id, None)
        await interaction.response.send_message("Removed.", ephemeral=True)

    async def _fire_triggers(self, event: str, guild: discord.Guild) -> None:
        """Run every trigger registered for an event in this guild, acting as each trigger's creator.
        :param event: the event name that just happened
        :param guild: the guild it happened in; only that guild's own triggers are considered
        """
        rows = await self.bot.db.fetch(
            "SELECT * FROM meta_triggers WHERE event = $1 AND guild_id = $2",
            event,
            guild.id,
        )
        for row in rows:
            channel = self.bot.get_channel(row["channel_id"])
            if channel is None:
                continue
            user = self._actor(guild, row["created_by"])
            ctx = ActionContext(
                bot=self.bot, guild=guild, channel=channel, user=user, interaction=None
            )
            try:
                await execute_chain(ctx, parse_chain(row["chain"]))
            except Exception:
                logger.exception("Error firing trigger %s", row["id"])

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Fire any triggers registered for member_join."""
        await self._fire_triggers("member_join", member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Fire any triggers registered for member_remove."""
        await self._fire_triggers("member_remove", member.guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Fire any message triggers watching this message's channel, skipping bot authors to avoid self-loops."""
        if message.author.bot or message.guild is None:
            return

        for row in self._message_triggers.get(message.channel.id, []):
            if (
                row["contains"]
                and row["contains"].lower() not in message.content.lower()
            ):
                continue
            channel = self.bot.get_channel(row["channel_id"])
            if channel is None:
                continue
            user = self._actor(message.guild, row["created_by"])
            ctx = ActionContext(
                bot=self.bot,
                guild=message.guild,
                channel=channel,
                user=user,
                interaction=None,
            )
            try:
                await execute_chain(ctx, parse_chain(row["chain"]))
            except Exception:
                logger.exception("Error firing trigger %s", row["id"])


async def setup(bot: StarbrightBot) -> None:
    """Entry point discord.py calls to load this cog.
    :param bot: the running bot instance
    """
    await bot.add_cog(MetaCommands(bot))

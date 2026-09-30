from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core.api.sse import ReplayGap
from cogs.settings import BackButton
from core.bot import StarbrightBot
from core.models.hail import HailArrival, HailRegion, canonical, display_name
from core.models.sse_event import SseEvent
from core.permissions import PermissionTier
from core.settings import SettingDefinition, SettingType

logger = logging.getLogger(__name__)

_CATEGORY = "Hail"
_REGIONS_CATEGORY = "Hail Regions"
_ROLE_KEY = "hail.role"

# An embed description holds 4096 characters; leave room for the code fence and quote tags.
_DESCRIPTION_LIMIT = 3900

def _utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def _quote_blocks(nations: list[str]) -> list[str]:
    """Format nations as RMB quote blocks in code fences, splitting into as many blocks as the embed limit needs.
    :param nations: canonical nation IDs
    :return: one code-fenced block per embed, in order
    """
    groups: list[list[str]] = [[]]
    length = 0
    for nation in nations:
        tag = f"[nation]{nation}[/nation]"
        if groups[-1] and length + len(tag) + 2 > _DESCRIPTION_LIMIT:
            groups.append([])
            length = 0
        groups[-1].append(tag)
        length += len(tag) + 2
    return ["```\n[quote=0;0]\n" + ", ".join(group) + "\n[/quote]\n```" for group in groups]


class Hail(commands.Cog):
    """/hail: list the nations that have arrived in tracked regions since the last time it was run.

    The tracked regions are managed from the "Hail Regions" page of /settings.
    """

    def __init__(self, bot: StarbrightBot):
        """
        :param bot: the running bot instance
        """
        self.bot = bot
        self._regions: set[str] = set()
        # region -> arrivals not yet listed by /hail, for the settings page (which can't query the database itself)
        self.waiting: dict[str, int] = {}
        # Serialises /hail, catch-ups and resets so two of them can't list or reseed the same arrivals.
        self._lock = asyncio.Lock()
        self._unsubscribe: list = []
        self._catch_up_task: Optional[asyncio.Task[None]] = None
        bot.settings.register(
            SettingDefinition(
                key=_ROLE_KEY,
                category=_CATEGORY,
                label="Hail role",
                description="The role allowed to use /hail. Moderators and administrators always can.",
                type=SettingType.ROLE,
                min_tier=PermissionTier.ADMINISTRATOR,
                optional=True,
            )
        )
        bot.settings.register_page(_REGIONS_CATEGORY, lambda interaction: HailRegionsView(self))

    async def cog_load(self) -> None:
        """Load the tracked regions, subscribe to the SSE feed, and schedule a catch-up for the time we were offline."""
        self._regions = {r.region for r in await HailRegion.all(self.bot.db)}
        self.waiting = await HailRegion.waiting_counts(self.bot.db)
        self._unsubscribe = [
            self.bot.sse.subscribe(self._on_movement, {"move", "founding"}),
            self.bot.sse.on_gap(self._on_gap),
        ]
        self._start_catch_up()

    async def cog_unload(self) -> None:
        """Stop listening to the SSE feed and cancel any catch-up in progress."""
        for cancel in self._unsubscribe:
            cancel()
        if self._catch_up_task is not None:
            self._catch_up_task.cancel()

    # ---- staying in sync with the feed ----

    async def _on_movement(self, event: SseEvent) -> None:
        """Record a nation entering a tracked region.
        :param event: a move or founding happening
        """
        movement = event.movement
        if movement is None or movement.destination not in self._regions:
            return
        try:
            if await HailArrival.record(
                self.bot.db, movement.destination, movement.nation, event.time, event.id
            ):
                self.waiting[movement.destination] = self.waiting.get(movement.destination, 0) + 1
        except Exception:
            logger.exception("Failed to record arrival of %s", movement.nation)

    async def _on_gap(self, gap: ReplayGap) -> None:
        """Reconcile after the feed said it couldn't replay everything missed.
        :param gap: the gap notice
        """
        logger.warning("Catching up on hail regions after a feed gap (%s)", gap.reason)
        self._start_catch_up()

    def _start_catch_up(self) -> None:
        """Schedule a catch-up, unless one is already running."""
        if self._catch_up_task is None or self._catch_up_task.done():
            self._catch_up_task = asyncio.create_task(self._catch_up())

    async def _residents(self, region: str) -> list[str]:
        """Fetch the nations currently in a region.
        :param region: canonical region ID
        :return: canonical nation IDs
        :raises aiohttp.ClientResponseError: if the region doesn't exist
        :raises RuntimeError: if NationStates answered with something other than region data
        """
        data = await self.bot.api.request({"region": region, "q": "nations"})
        if "REGION" not in data:
            raise RuntimeError(f"unexpected response for region {region}: {data}")
        raw = data["REGION"].get("NATIONS") or ""
        return [nation for nation in raw.split(":") if nation]

    async def _catch_up(self) -> None:
        """Record nations that are in a tracked region but were never seen arriving (missed while offline)."""
        await self.bot.wait_until_ready()
        for region in sorted(self._regions):
            try:
                residents = await self._residents(region)
                async with self._lock:
                    if region in self._regions:
                        await HailArrival.record_many(
                            self.bot.db, region, residents, _utc_now()
                        )
                        self.waiting = await HailRegion.waiting_counts(self.bot.db)
            except Exception:
                logger.exception("Catch-up failed for region %s", region)

    # ---- /hail ----

    def _can_hail(self, member: discord.abc.User) -> bool:
        """Check whether a member may use /hail.
        :param member: the member running the command
        :return: True if they hold the configured role or are at least a moderator
        """
        if self.bot.permissions.has_tier(member, PermissionTier.MODERATOR):
            return True
        role_id = self.bot.settings.get(_ROLE_KEY)
        return bool(role_id) and any(
            role.id == role_id for role in getattr(member, "roles", [])
        )

    async def _region_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest tracked regions.
        :param interaction: the interaction being completed
        :param current: what the user has typed so far
        :return: up to 25 matching regions
        """
        typed = canonical(current)
        return [
            app_commands.Choice(name=display_name(region), value=region)
            for region in sorted(self._regions)
            if typed in region
        ][:25]

    @app_commands.command(
        name="hail",
        description="List nations that have arrived in a tracked region since the last hail.",
    )
    @app_commands.guild_only()
    @app_commands.autocomplete(region=_region_autocomplete)
    async def hail(
        self, interaction: discord.Interaction, region: Optional[str] = None
    ) -> None:
        """List new arrivals, then mark them as listed so the next hail starts after them.
        :param interaction: the invoking interaction
        :param region: a tracked region to hail; all tracked regions if omitted
        """
        if not self._can_hail(interaction.user):
            await interaction.response.send_message(
                "You aren't allowed to use /hail.", ephemeral=True
            )
            return
        if region is not None:
            region = canonical(region)
            if region not in self._regions:
                await interaction.response.send_message(
                    f"{display_name(region)} isn't a tracked region.", ephemeral=True
                )
                return
        regions = [region] if region else sorted(self._regions)
        if not regions:
            await interaction.response.send_message(
                "No regions are tracked yet. An administrator can add one under Hail Regions in /settings.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        async with self._lock:
            for name in regions:
                await self._hail_region(interaction, name)

    async def _hail_region(self, interaction: discord.Interaction, region: str) -> None:
        """Send one region's new arrivals and advance its cursor once they have all been sent.
        :param interaction: the invoking interaction, already deferred
        :param region: canonical region ID
        """
        tracked = await HailRegion.get(self.bot.db, region)
        if tracked is None:
            return
        arrivals = await HailArrival.unlisted(self.bot.db, region)
        name = display_name(region)
        if not arrivals:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=name,
                    description="No new arrivals since the last hail.",
                    colour=discord.Colour.blurple(),
                )
            )
            return

        blocks = _quote_blocks([a.nation for a in arrivals])
        title = f"{name}: {len(arrivals)} new arrival{'s' if len(arrivals) != 1 else ''}"
        for index, block in enumerate(blocks, start=1):
            page = f" ({index}/{len(blocks)})" if len(blocks) > 1 else ""
            await interaction.followup.send(
                embed=discord.Embed(
                    title=title + page, description=block, colour=discord.Colour.blurple()
                )
            )
        await tracked.mark_listed(self.bot.db, max(a.id for a in arrivals))
        self.waiting[region] = 0

    # ---- region management, driven by the "Hail Regions" settings page ----

    async def add_region(self, name: str) -> str:
        """Track a region. Its current residents count as already seen, so the first hail lists nobody.
        :param name: the region's name
        :return: a status message for the user
        """
        region = canonical(name)
        if not region:
            return "Enter a region name."
        if region in self._regions:
            return f"{display_name(region)} is already tracked."
        try:
            residents = await self._residents(region)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                return f"NationStates has no region called {display_name(region)}."
            return f"NationStates returned an error ({exc.status}); try again shortly."
        async with self._lock:
            await HailRegion.seed(self.bot.db, region, residents, _utc_now())
            self._regions.add(region)
            self.waiting[region] = 0
        return f"✅ Now tracking **{display_name(region)}** ({len(residents)} current nations marked as seen)."

    async def reset_region(self, region: str) -> str:
        """Reseed a region's seen list from who is there right now.
        :param region: canonical region ID
        :return: a status message for the user
        """
        if region not in self._regions:
            return f"{display_name(region)} isn't tracked."
        try:
            residents = await self._residents(region)
        except Exception:
            logger.exception("Reset failed for region %s", region)
            return "Couldn't fetch the region from NationStates; nothing was changed."
        async with self._lock:
            await HailRegion.seed(self.bot.db, region, residents, _utc_now())
            self.waiting[region] = 0
        return f"✅ Reset **{display_name(region)}**: {len(residents)} current nations marked as seen."

    async def remove_region(self, region: str) -> str:
        """Stop tracking a region and forget the nations seen in it.
        :param region: canonical region ID
        :return: a status message for the user
        """
        async with self._lock:
            tracked = await HailRegion.get(self.bot.db, region)
            if tracked is None:
                return f"{display_name(region)} isn't tracked."
            self._regions.discard(region)
            self.waiting.pop(region, None)
            await tracked.delete(self.bot.db)
        return f"✅ No longer tracking **{display_name(region)}**."


# Discord caps a select menu at 25 options.
_MAX_REGION_OPTIONS = 25

_ACTIONS = {
    "reset": ("Reset", "Treat everyone in {region} right now as seen, clearing what is waiting to be hailed?"),
    "remove": ("Remove", "Stop tracking {region} and forget every nation seen in it?"),
}


def _may_manage_regions(bot: StarbrightBot, user: discord.abc.User) -> bool:
    """Check whether a user may change which regions are tracked.
    :param bot: the running bot instance
    :param user: the user attempting the change
    :return: True if they hold the Administrator tier
    """
    return bot.permissions.has_tier(user, PermissionTier.ADMINISTRATOR)


async def _deny(interaction: discord.Interaction) -> None:
    """Tell a user they can't manage tracked regions.
    :param interaction: the interaction to answer
    """
    await interaction.response.send_message(
        "You need Administrator permissions to change the tracked regions.", ephemeral=True
    )


class RegionSelect(discord.ui.Select):
    """Picks which tracked region the Reset and Remove buttons act on."""

    def __init__(self, screen: HailRegionsView, regions: list[str]):
        """
        :param screen: the regions screen
        :param regions: the tracked regions to offer
        """
        options = [
            discord.SelectOption(
                label=display_name(region),
                value=region,
                description=f"{screen.cog.waiting.get(region, 0)} waiting",
                default=region == screen.selected,
            )
            for region in regions
        ]
        super().__init__(placeholder="Choose a region to reset or remove...", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Select the chosen region.
        :param interaction: the select interaction
        """
        await interaction.response.edit_message(view=self.view.redraw(selected=self.values[0]))


class AddRegionModal(discord.ui.Modal):
    """Asks for the name of a region to start tracking."""

    def __init__(self, screen: HailRegionsView):
        """
        :param screen: the regions screen to update afterwards
        """
        super().__init__(title="Track a region")
        self.screen = screen
        self.name = discord.ui.TextInput(label="Region name", placeholder="e.g. The Pacific", max_length=60)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Start tracking the region and redraw the screen with the outcome.
        :param interaction: the modal submission interaction
        """
        if not _may_manage_regions(self.screen.bot, interaction.user):
            await _deny(interaction)
            return
        await interaction.response.defer()
        notice = await self.screen.cog.add_region(self.name.value)
        await interaction.edit_original_response(view=self.screen.redraw(notice=notice))


class AddRegionButton(discord.ui.Button):
    """Opens the modal for tracking a new region."""

    def __init__(self):
        super().__init__(label="Add region", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Show the modal.
        :param interaction: the button click interaction
        """
        if not _may_manage_regions(self.view.bot, interaction.user):
            await _deny(interaction)
            return
        await interaction.response.send_modal(AddRegionModal(self.view))


class RegionActionButton(discord.ui.Button):
    """Asks for confirmation before resetting or removing the selected region."""

    def __init__(self, action: str, disabled: bool):
        """
        :param action: "reset" or "remove"
        :param disabled: whether no region is selected
        """
        label, _ = _ACTIONS[action]
        super().__init__(
            label=label,
            style=discord.ButtonStyle.danger if action == "remove" else discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        """Redraw the screen asking to confirm the action.
        :param interaction: the button click interaction
        """
        if not _may_manage_regions(self.view.bot, interaction.user):
            await _deny(interaction)
            return
        await interaction.response.edit_message(view=self.view.redraw(confirming=self.action))


class ConfirmButton(discord.ui.Button):
    """Carries out the action the screen is asking to confirm."""

    def __init__(self):
        super().__init__(label="Confirm", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Run the action on the selected region and redraw the screen with the outcome.
        :param interaction: the button click interaction
        """
        screen: HailRegionsView = self.view
        if not _may_manage_regions(screen.bot, interaction.user):
            await _deny(interaction)
            return
        await interaction.response.defer()
        region = screen.selected
        if screen.confirming == "remove":
            notice = await screen.cog.remove_region(region)
            selected = None
        else:
            notice = await screen.cog.reset_region(region)
            selected = region
        await interaction.edit_original_response(
            view=screen.redraw(selected=selected, confirming=None, notice=notice)
        )


class CancelButton(discord.ui.Button):
    """Backs out of a pending confirmation."""

    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Redraw the screen without the confirmation.
        :param interaction: the button click interaction
        """
        await interaction.response.edit_message(view=self.view.redraw(confirming=None))


class HailRegionsView(discord.ui.LayoutView):
    """Settings screen for the regions /hail tracks. Unlike other settings, changes here apply immediately."""

    def __init__(
        self,
        cog: Hail,
        selected: Optional[str] = None,
        confirming: Optional[str] = None,
        notice: Optional[str] = None,
    ):
        """
        :param cog: the Hail cog, which holds the tracked regions
        :param selected: the region the Reset and Remove buttons act on, if one is chosen
        :param confirming: "reset" or "remove" while waiting for the user to confirm it
        :param notice: a one-line status message to show under the title
        """
        super().__init__(timeout=180)
        self.cog = cog
        self.bot = cog.bot
        regions = sorted(cog._regions)
        self.selected = selected if selected in cog._regions else None
        self.confirming = confirming if self.selected else None

        title = f"## {_REGIONS_CATEGORY}\nRegions whose new arrivals /hail lists. Changes here apply immediately."
        if notice:
            title += f"\n-# {notice}"

        if regions:
            shown = regions[:_MAX_REGION_OPTIONS]
            lines = [f"- **{display_name(r)}** · {cog.waiting.get(r, 0)} waiting" for r in shown]
            if len(regions) > len(shown):
                lines.append(f"-# …and {len(regions) - len(shown)} more not shown")
            summary = "\n".join(lines)
        else:
            summary = "*No regions are tracked yet. Press Add region to start.*"

        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(title))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(summary))
        container.add_item(discord.ui.Separator())

        if self.confirming:
            _, question = _ACTIONS[self.confirming]
            container.add_item(discord.ui.TextDisplay(question.format(region=f"**{display_name(self.selected)}**")))
            container.add_item(discord.ui.ActionRow(ConfirmButton(), CancelButton()))
        else:
            if regions:
                container.add_item(discord.ui.ActionRow(RegionSelect(self, regions[:_MAX_REGION_OPTIONS])))
            container.add_item(
                discord.ui.ActionRow(
                    BackButton(self.bot, disabled=False),
                    AddRegionButton(),
                    RegionActionButton("reset", disabled=self.selected is None),
                    RegionActionButton("remove", disabled=self.selected is None),
                )
            )
        self.add_item(container)

    def redraw(self, **changes) -> HailRegionsView:
        """Build a fresh copy of this screen with some of its state changed.
        :param changes: any of selected, confirming, notice
        :return: the new screen
        """
        state = {"selected": self.selected, "confirming": self.confirming}
        state.update(changes)
        return HailRegionsView(self.cog, **state)

async def setup(bot: StarbrightBot) -> None:
    """Entry point discord.py calls to load this cog.
    :param bot: the running bot instance
    """
    await bot.add_cog(Hail(bot))

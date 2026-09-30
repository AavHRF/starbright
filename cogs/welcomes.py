from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

import discord
from discord.ext import commands

from cogs.settings import BackButton
from core.bot import StarbrightBot
from core.permissions import PermissionTier
from core.settings import SettingDefinition, SettingsRegistry, SettingType, SettingValue

logger = logging.getLogger(__name__)

# How long after an INVITE_DELETE a vanished invite may still be credited with a join. Discord deletes an
# invite the moment it hits max_uses, and that delete can arrive just before or just after the member join.
_CONSUMED_INVITE_WINDOW = 30.0

_MESSAGE_CATEGORY = "Welcome Message"
_DELIVERY_CATEGORY = "Welcome Delivery"

_MAX_LINKS = 10

_INVITE_CATEGORY = "Invite Management"

# Discord caps a select menu at 25 options, so the invite picker pages through them.
_INVITES_PER_PAGE = 25


def _length_limit(label: str, limit: int) -> Callable[[SettingValue], SettingValue]:
    """Build a normalizer that rejects text longer than a limit.
    :param label: the setting's name, for the error message
    :param limit: the maximum number of characters
    :return: a normalizer for SettingDefinition.normalize
    """

    def normalize(value: SettingValue) -> SettingValue:
        if len(str(value)) > limit:
            raise ValueError(f"{label} can be at most {limit} characters.")
        return value

    return normalize


def _normalize_url(value: SettingValue) -> SettingValue:
    """Reject anything that isn't an http(s) URL.
    :param value: the entered URL
    :return: the URL, unchanged
    """
    url = str(value)
    if not url.startswith(("http://", "https://")) or any(c.isspace() for c in url):
        raise ValueError(f"`{url}` isn't a valid link; it must start with http:// or https://.")
    return url


def _normalize_color(value: SettingValue) -> SettingValue:
    """Accept a colour as hex (#5865F2, 0x5865F2) or rgb(88, 101, 242) and store it as #RRGGBB.
    :param value: the entered colour
    :return: the colour as an uppercase #RRGGBB string
    """
    try:
        colour = discord.Colour.from_str(str(value))
    except ValueError:
        raise ValueError(f"`{value}` isn't a colour; use a hex code like `#5865F2`.") from None
    return f"#{colour.value:06X}"


def _parse_links(raw: str) -> list[tuple[str, str]]:
    """Parse the links setting: one `Label | URL` pair per line.
    :param raw: the setting's stored or entered text
    :return: (label, url) pairs, in order
    """
    links = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        label, separator, url = line.partition("|")
        if not separator or not label.strip():
            raise ValueError(f"`{line.strip()}` should be written as `Label | https://link`.")
        if len(label.strip()) > 80:
            raise ValueError(f"Link labels can be at most 80 characters: `{label.strip()}`.")
        links.append((label.strip(), _normalize_url(url.strip())))
    if len(links) > _MAX_LINKS:
        raise ValueError(f"The welcome message can have at most {_MAX_LINKS} links.")
    return links


def _normalize_links(value: SettingValue) -> SettingValue:
    """Validate the links setting and tidy its spacing.
    :param value: the entered text
    :return: one `Label | URL` per line, or None if there were no links
    """
    links = _parse_links(str(value))
    return "\n".join(f"{label} | {url}" for label, url in links) or None


def _register_settings(settings: SettingsRegistry) -> None:
    """Register every welcome setting, all optional, with a preview of the welcome message on both screens.
    :param settings: the core settings registry
    """
    definitions = [
        SettingDefinition(
            key="welcome.channel",
            category=_DELIVERY_CATEGORY,
            label="Welcome channel",
            description="Where new members are welcomed. Leave unset to turn welcome messages off.",
            type=SettingType.CHANNEL,
        ),
        SettingDefinition(
            key="welcome.ping_role",
            category=_DELIVERY_CATEGORY,
            label="Welcomer role",
            description="Pinged above each welcome message to alert the welcome team.",
            type=SettingType.ROLE,
        ),
        SettingDefinition(
            key="welcome.header",
            category=_MESSAGE_CATEGORY,
            label="Header",
            description="The title at the top. Use {member} and {server} for the new member and server name.",
            type=SettingType.STRING,
            normalize=_length_limit("The header", 256),
        ),
        SettingDefinition(
            key="welcome.header_link",
            category=_MESSAGE_CATEGORY,
            label="Header link",
            description="Makes the header a link to this URL.",
            type=SettingType.STRING,
            normalize=_normalize_url,
        ),
        SettingDefinition(
            key="welcome.text",
            category=_MESSAGE_CATEGORY,
            label="Text",
            description="The body of the message. Use {member} and {server} for the new member and server name.",
            type=SettingType.STRING,
            multiline=True,
            normalize=_length_limit("The text", 2000),
        ),
        SettingDefinition(
            key="welcome.links",
            category=_MESSAGE_CATEGORY,
            label="Links",
            description=f"Link buttons, one per line as `Label | https://link` (up to {_MAX_LINKS}).",
            type=SettingType.STRING,
            multiline=True,
            normalize=_normalize_links,
        ),
        SettingDefinition(
            key="welcome.image",
            category=_MESSAGE_CATEGORY,
            label="Image",
            description="A link to an image shown below the text.",
            type=SettingType.STRING,
            normalize=_normalize_url,
        ),
        SettingDefinition(
            key="welcome.color",
            category=_MESSAGE_CATEGORY,
            label="Sidebar color",
            description="The accent strip down the left side, as a hex code like #5865F2.",
            type=SettingType.STRING,
            normalize=_normalize_color,
        ),
        SettingDefinition(
            key="welcome.footer",
            category=_MESSAGE_CATEGORY,
            label="Footer",
            description="Small text at the bottom. Use {member} and {server} for the new member and server name.",
            type=SettingType.STRING,
            normalize=_length_limit("The footer", 500),
        ),
    ]
    for definition in definitions:
        definition.optional = True
        settings.register(definition)
    settings.register_preview(_MESSAGE_CATEGORY, build_welcome)
    settings.register_preview(_DELIVERY_CATEGORY, build_welcome)


def _fill(text: str, member: discord.Member) -> str:
    """Substitute the welcome placeholders.
    :param text: the configured text
    :param member: the member being welcomed
    :return: the text with {member} and {server} filled in
    """
    return text.replace("{member}", member.mention).replace("{server}", member.guild.name)


def build_welcome(
    member: discord.Member, get: Callable[[str], SettingValue]
) -> list[discord.ui.Item]:
    """Render the welcome message for a member: the welcomer role ping, then the message itself in a container.
    :param member: the member being welcomed
    :param get: returns a welcome setting's value
    :return: the message's top-level components, empty if nothing is configured
    """
    children: list[discord.ui.Item] = []

    header = get("welcome.header")
    if header:
        header = _fill(header, member)
        link = get("welcome.header_link")
        children.append(discord.ui.TextDisplay(f"## [{header}]({link})" if link else f"## {header}"))

    text = get("welcome.text")
    if text:
        children.append(discord.ui.TextDisplay(_fill(text, member)))

    image = get("welcome.image")
    if image:
        children.append(discord.ui.MediaGallery(discord.MediaGalleryItem(image)))

    links = _parse_links(get("welcome.links") or "")
    for start in range(0, len(links), 5):
        children.append(
            discord.ui.ActionRow(
                *(discord.ui.Button(label=label, url=url) for label, url in links[start : start + 5])
            )
        )

    footer = get("welcome.footer")
    if footer:
        if children:
            children.append(discord.ui.Separator())
        children.append(discord.ui.TextDisplay(f"-# {_fill(footer, member)}"))

    items: list[discord.ui.Item] = []
    role_id = get("welcome.ping_role")
    if role_id:
        items.append(discord.ui.TextDisplay(f"<@&{role_id}>"))
    if children:
        color = get("welcome.color")
        items.append(
            discord.ui.Container(
                *children, accent_colour=discord.Colour.from_str(color) if color else None
            )
        )
    return items


@dataclass
class InviteResolution:

    candidates: list[discord.Invite] = field(default_factory=list)

    @property
    def invite(self) -> Optional[discord.Invite]:
        """The invite used, or None if it couldn't be pinned down to exactly one."""
        return self.candidates[0] if len(self.candidates) == 1 else None

    @property
    def ambiguous(self) -> bool:
        """True if more than one invite's uses went up since the last snapshot."""
        return len(self.candidates) > 1


def _has_expired(invite: discord.Invite, now: datetime) -> bool:
    """Check whether an invite's lifetime has run out.
    :param invite: the invite
    :param now: the current time, as a timezone-aware UTC datetime
    :return: True if the invite had an expiry time and it has passed
    """
    expires_at = invite.expires_at
    # Invites from gateway events carry max_age but not always expires_at.
    if expires_at is None and invite.max_age and invite.created_at:
        expires_at = invite.created_at + timedelta(seconds=invite.max_age)
    return expires_at is not None and expires_at <= now


def _plural(count: int, noun: str) -> str:
    """Format a count with its noun, pluralized.
    :param count: how many
    :param noun: the singular noun
    :return: e.g. '1 role' or '3 roles'
    """
    return f"{count} {noun}{'s' if count != 1 else ''}"


def _may_manage_invites(bot: StarbrightBot, user: discord.abc.User) -> bool:
    """Check whether a user may change which roles invites grant.
    :param bot: the running bot instance
    :param user: the user attempting the change
    :return: True if they hold the Administrator tier
    """
    return bot.permissions.has_tier(user, PermissionTier.ADMINISTRATOR)


class InviteSelect(discord.ui.Select):
    """Picks which invite to edit the roles of, from one page of the guild's invites."""

    def __init__(self, screen: InviteRolesView, invites: list[discord.Invite]):
        """
        :param screen: the invite management screen
        :param invites: the invites on the current page
        """
        options = []
        for invite in invites:
            details = [
                f"by {invite.inviter.display_name}" if invite.inviter else "unknown creator",
                f"{invite.uses or 0}/{invite.max_uses} uses" if invite.max_uses else f"{invite.uses or 0} uses",
            ]
            role_count = len(screen.roles_for(invite.code))
            if role_count:
                details.append(_plural(role_count, "role"))
            options.append(
                discord.SelectOption(
                    label=invite.code,
                    description=" · ".join(details)[:100],
                    default=invite.code == screen.selected,
                )
            )
        super().__init__(placeholder="Choose an invite...", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Show the chosen invite's roles.
        :param interaction: the select interaction
        """
        await interaction.response.edit_message(view=self.view.redraw(selected=self.values[0]))


class InviteRoleSelect(discord.ui.RoleSelect):
    """Picks the roles the selected invite grants to members who join through it."""

    def __init__(self, code: str, current: list[int]):
        """
        :param code: the invite being edited
        :param current: the roles it grants, including any unsaved change
        """
        super().__init__(
            placeholder=f"Choose roles for {code}...",
            min_values=0,
            max_values=25,
            default_values=[discord.Object(id=role_id) for role_id in current],
        )
        self.code = code

    async def callback(self, interaction: discord.Interaction) -> None:
        """Stage the chosen roles, rejecting any the bot can't hand out.
        :param interaction: the select interaction
        """
        screen: InviteRolesView = self.view
        if not _may_manage_invites(screen.bot, interaction.user):
            await interaction.response.send_message(
                "You need Administrator permissions to change invite roles.", ephemeral=True
            )
            return
        unassignable = [role for role in self.values if not role.is_assignable()]
        if unassignable:
            await interaction.response.send_message(
                f"I can't assign {', '.join(role.mention for role in unassignable)}. Roles must be below my "
                "highest role, and can't be managed by an integration.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        pending = dict(screen.pending)
        role_ids = sorted(role.id for role in self.values)
        if role_ids == sorted(screen.cog.invite_roles.get(self.code, [])):
            pending.pop(self.code, None)
        else:
            pending[self.code] = role_ids
        await interaction.response.edit_message(view=screen.redraw(pending=pending))


class InvitePageButton(discord.ui.Button):
    """Moves the invite picker to the previous or next page."""

    def __init__(self, step: int, disabled: bool):
        """
        :param step: -1 for the previous page, 1 for the next
        :param disabled: whether there's no page in that direction
        """
        super().__init__(label="◀" if step < 0 else "▶", disabled=disabled)
        self.step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        """Redraw the screen on the adjacent page.
        :param interaction: the button click interaction
        """
        screen: InviteRolesView = self.view
        await interaction.response.edit_message(view=screen.redraw(page=screen.page + self.step))


class InviteSaveButton(discord.ui.Button):
    """Saves every staged invite role change."""

    def __init__(self, disabled: bool):
        """
        :param disabled: whether to disable it, when there's nothing to save
        """
        super().__init__(label="Save", style=discord.ButtonStyle.success, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Persist the staged changes and redraw the screen.
        :param interaction: the button click interaction
        """
        screen: InviteRolesView = self.view
        if not _may_manage_invites(screen.bot, interaction.user):
            await interaction.response.send_message(
                "You need Administrator permissions to change invite roles.", ephemeral=True
            )
            return
        await screen.cog.save_invite_roles(screen.pending)
        await interaction.response.edit_message(
            view=screen.redraw(
                pending={}, notice=f"✅ Saved {_plural(len(screen.pending), 'change')}."
            )
        )


class InviteRejectButton(discord.ui.Button):
    """Discards every staged invite role change."""

    def __init__(self, disabled: bool):
        """
        :param disabled: whether to disable it, when there's nothing to discard
        """
        super().__init__(label="Reject", style=discord.ButtonStyle.danger, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Redraw the screen with the saved roles.
        :param interaction: the button click interaction
        """
        await interaction.response.edit_message(
            view=self.view.redraw(pending={}, notice="Discarded unsaved changes.")
        )


class InviteRolesView(discord.ui.LayoutView):
    """Settings screen for choosing which roles each invite grants, staging edits until Save or Reject."""

    def __init__(
        self,
        cog: Welcomes,
        guild: discord.Guild,
        pending: Optional[dict[str, list[int]]] = None,
        selected: Optional[str] = None,
        page: int = 0,
        notice: Optional[str] = None,
    ):
        """
        :param cog: the Welcomes cog, which holds the invite cache and saved invite roles
        :param guild: the guild whose invites are shown
        :param pending: unsaved role changes carried over from the previous redraw, by invite code
        :param selected: the invite whose roles are being edited, if one is chosen
        :param page: which page of invites the picker shows
        :param notice: a one-line status message to show under the title
        """
        super().__init__(timeout=180)
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.pending = pending or {}
        self.selected = selected

        invites = sorted(
            cog.cached_invites(guild).values(),
            key=lambda invite: invite.created_at.timestamp() if invite.created_at else 0,
            reverse=True,
        )
        page_count = max(1, -(-len(invites) // _INVITES_PER_PAGE))
        self.page = min(max(page, 0), page_count - 1)
        by_code = {invite.code: invite for invite in invites}

        title = f"## {_INVITE_CATEGORY} Settings\nNew members get the roles of the invite they most likely joined through."
        if self.pending:
            title += f"\n-# {_plural(len(self.pending), 'unsaved change')}. Press Save to apply."
        elif notice:
            title += f"\n-# {notice}"

        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(title))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._summary(by_code)))
        container.add_item(discord.ui.Separator())

        if not invites:
            container.add_item(
                discord.ui.TextDisplay(
                    "*No invites found. Make sure I have the Manage Server permission.*"
                )
            )
        else:
            start = self.page * _INVITES_PER_PAGE
            container.add_item(
                discord.ui.ActionRow(InviteSelect(self, invites[start : start + _INVITES_PER_PAGE]))
            )

        invite = by_code.get(selected) if selected else None
        if invite is not None:
            details = [f"**{invite.code}**"]
            if invite.inviter:
                details.append(f"created by {invite.inviter.mention}")
            if invite.channel:
                details.append(f"to <#{invite.channel.id}>")
            details.append(
                f"{invite.uses or 0}/{invite.max_uses} uses" if invite.max_uses else f"{invite.uses or 0} uses"
            )
            if invite.expires_at:
                details.append(f"expires {discord.utils.format_dt(invite.expires_at, 'R')}")
            container.add_item(discord.ui.TextDisplay(" · ".join(details)))
            container.add_item(discord.ui.ActionRow(InviteRoleSelect(invite.code, self.roles_for(invite.code))))

        container.add_item(discord.ui.Separator())
        controls = discord.ui.ActionRow(
            BackButton(self.bot, disabled=bool(self.pending)),
            InviteSaveButton(disabled=not self.pending),
            InviteRejectButton(disabled=not self.pending),
        )
        if page_count > 1:
            controls.add_item(InvitePageButton(-1, disabled=self.page == 0))
            controls.add_item(InvitePageButton(1, disabled=self.page >= page_count - 1))
        container.add_item(controls)
        self.add_item(container)

    def roles_for(self, code: str) -> list[int]:
        """Return the roles an invite grants as shown on this screen, including any unsaved change.
        :param code: the invite code
        :return: role IDs
        """
        return self.pending[code] if code in self.pending else self.cog.invite_roles.get(code, [])

    def _summary(self, by_code: dict[str, discord.Invite]) -> str:
        """List every invite that grants roles.
        :param by_code: the guild's current invites, by code
        :return: one line per invite, marking unsaved changes and invites that no longer exist
        """
        codes = sorted(set(self.cog.invite_roles) | set(self.pending))
        lines = []
        for code in codes:
            role_ids = self.roles_for(code)
            if not role_ids and code not in self.pending:
                continue
            roles = ", ".join(f"<@&{role_id}>" for role_id in role_ids) or "*no roles*"
            line = f"`{code}` → {roles}"
            if code not in by_code:
                line += " · *invite no longer exists*"
            if code in self.pending:
                line += " · *unsaved*"
            lines.append(line)
        if not lines:
            return "**Invite roles**\n*No invites grant roles yet. Choose one below to add some.*"
        text = "**Invite roles**\n"
        for index, line in enumerate(lines):
            if len(text) + len(line) > 1500:
                text += f"-# …and {len(lines) - index} more"
                break
            text += line + "\n"
        return text.rstrip()

    def redraw(self, **changes) -> InviteRolesView:
        """Build a fresh copy of this screen with some of its state changed.
        :param changes: any of pending, selected, page, notice
        :return: the new screen
        """
        state = {"pending": self.pending, "selected": self.selected, "page": self.page}
        state.update(changes)
        return InviteRolesView(self.cog, self.guild, **state)


class Welcomes(commands.Cog):
    """Greets new members and tracks which invite each one joined through."""

    def __init__(self, bot: StarbrightBot):
        """
        :param bot: the running bot instance
        """
        self.bot = bot
        _register_settings(bot.settings)
        bot.settings.register_page(
            _INVITE_CATEGORY, lambda interaction: InviteRolesView(self, interaction.guild)
        )
        # invite code -> role IDs granted to members who join through it
        self.invite_roles: dict[str, list[int]] = {}
        # guild ID -> invite code -> invite as of the last snapshot
        self._invites: dict[int, dict[str, discord.Invite]] = {}
        # guild ID -> invite code -> monotonic time its INVITE_DELETE arrived
        self._deleted_at: dict[int, dict[str, float]] = defaultdict(dict)
        # Serializes join resolution per guild so back-to-back joins each diff against their own snapshot.
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        """Load the saved invite roles."""
        rows = await self.bot.db.fetch("SELECT invite_code, role_id FROM invite_roles")
        for row in rows:
            self.invite_roles.setdefault(row["invite_code"], []).append(row["role_id"])

    def cached_invites(self, guild: discord.Guild) -> dict[str, discord.Invite]:
        """Return a guild's invites as of the last snapshot.
        :param guild: the guild
        :return: its invites by code, empty if they couldn't be fetched
        """
        return self._invites.get(guild.id, {})

    async def save_invite_roles(self, changes: dict[str, list[int]]) -> None:
        """Replace the roles granted by each changed invite.
        :param changes: the new role IDs for each invite code; an empty list removes all of its roles
        """
        async with self.bot.db.pool.acquire() as conn, conn.transaction():
            for code, role_ids in changes.items():
                await conn.execute("DELETE FROM invite_roles WHERE invite_code = $1", code)
                await conn.executemany(
                    "INSERT INTO invite_roles (invite_code, role_id) VALUES ($1, $2)",
                    [(code, role_id) for role_id in role_ids],
                )
        for code, role_ids in changes.items():
            if role_ids:
                self.invite_roles[code] = list(role_ids)
            else:
                self.invite_roles.pop(code, None)

    async def _prune_invite_roles(self) -> None:
        """Forget roles for invites that no longer exist, once every guild's invites are known."""
        if not all(guild.id in self._invites for guild in self.bot.guilds):
            return
        live = {code for invites in self._invites.values() for code in invites}
        stale = [code for code in self.invite_roles if code not in live]
        if stale:
            await self.save_invite_roles({code: [] for code in stale})
            logger.info("Removed roles for deleted invites: %s", ", ".join(stale))

    async def _assign_invite_roles(self, member: discord.Member, resolution: InviteResolution) -> None:
        """Give a new member the roles of the invite they joined through. If it's ambiguous which invite they
        used, give only the roles every candidate invite grants.
        :param member: the member who joined
        :param resolution: the invite(s) they could have joined through
        """
        if not resolution.candidates:
            return
        role_ids = set.intersection(
            *(set(self.invite_roles.get(invite.code, [])) for invite in resolution.candidates)
        )
        roles = [
            role
            for role_id in role_ids
            if (role := member.guild.get_role(role_id)) is not None and role.is_assignable()
        ]
        if not roles:
            return
        codes = ", ".join(invite.code for invite in resolution.candidates)
        try:
            await member.add_roles(*roles, reason=f"Joined via invite {codes}")
        except discord.HTTPException:
            logger.exception("Failed to assign invite roles to %s", member.id)

    async def _fetch_invites(self, guild: discord.Guild) -> Optional[dict[str, discord.Invite]]:
        """Fetch every current invite in a guild, keyed by code.
        :param guild: the guild to fetch from
        :return: the guild's invites, or None if the bot lacks Manage Server there or the fetch failed
        """
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            logger.warning("Missing Manage Server in %s; can't track invites there", guild.id)
            return None
        except discord.HTTPException:
            logger.exception("Couldn't fetch invites in %s", guild.id)
            return None
        return {invite.code: invite for invite in invites}

    async def _snapshot(self, guild: discord.Guild) -> None:
        """Replace a guild's cached invites with a fresh fetch.
        :param guild: the guild to snapshot
        """
        async with self._locks[guild.id]:
            invites = await self._fetch_invites(guild)
            if invites is not None:
                self._invites[guild.id] = invites
                self._deleted_at[guild.id].clear()

    async def resolve_invite(self, guild: discord.Guild) -> InviteResolution:
        """Work out which invite was just used to join a guild, and refresh the cache for the next join.
        :param guild: the guild the member joined
        :return: the invite(s) whose use count went up since the last snapshot
        """
        async with self._locks[guild.id]:
            previous = self._invites.get(guild.id)
            current = await self._fetch_invites(guild)
            if previous is None or current is None:
                if current is not None:
                    self._invites[guild.id] = current
                return InviteResolution()

            # Invites still around whose uses went up (an invite missing from the cache was created since
            # the snapshot without us seeing INVITE_CREATE, so it started at 0).
            candidates = []
            for code, invite in current.items():
                old = previous.get(code)
                if (invite.uses or 0) > (old.uses or 0 if old else 0):
                    candidates.append(invite)

            # Invites that vanished because this join used their last remaining use. Ones deleted outside the
            # window were removed by hand or expired, not consumed, and ones past their expiry time expired
            # even if their delete hasn't arrived yet.
            now = time.monotonic()
            utc_now = discord.utils.utcnow()
            deleted_at = self._deleted_at[guild.id]
            for code, invite in previous.items():
                if code in current or not invite.max_uses:
                    continue
                if (invite.uses or 0) + 1 != invite.max_uses:
                    continue
                if code in deleted_at and now - deleted_at[code] > _CONSUMED_INVITE_WINDOW:
                    continue
                if _has_expired(invite, utc_now):
                    continue
                candidates.append(invite)

            self._invites[guild.id] = current
            deleted_at.clear()
            return InviteResolution(candidates)

    async def _send_welcome(self, member: discord.Member) -> None:
        """Post the configured welcome message for a new member, if a welcome channel is set.
        :param member: the member who joined
        """
        channel_id = self.bot.settings.get("welcome.channel")
        channel = member.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            return
        items = build_welcome(member, self.bot.settings.get)
        if not items:
            return

        view = discord.ui.LayoutView(timeout=None)
        for item in items:
            view.add_item(item)
        role_id = self.bot.settings.get("welcome.ping_role")
        mentions = discord.AllowedMentions(
            everyone=False, users=True, roles=[discord.Object(id=role_id)] if role_id else False
        )
        try:
            await channel.send(view=view, allowed_mentions=mentions)
        except discord.HTTPException:
            logger.exception("Failed to send welcome message for %s", member.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Snapshot every guild's invites. Also fires after a full reconnect, when events may have been missed."""
        for guild in self.bot.guilds:
            await self._snapshot(guild)
        await self._prune_invite_roles()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Snapshot invites for a guild the bot was just added to."""
        await self._snapshot(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        """Add a new invite to the cache so its first use is attributed correctly."""
        if invite.guild is None:
            return
        async with self._locks[invite.guild.id]:
            cached = self._invites.get(invite.guild.id)
            if cached is not None:
                cached[invite.code] = invite

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        """Note when an invite was deleted, leaving it cached so a join that consumed its last use can still find it."""
        if invite.guild is None:
            return
        self._deleted_at[invite.guild.id][invite.code] = time.monotonic()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Work out which invite the new member used."""
        if member.bot:
            return
        resolution = await self.resolve_invite(member.guild)
        if resolution.invite is not None:
            inviter = resolution.invite.inviter
            logger.info(
                "%s joined %s via invite %s (created by %s)",
                member.id,
                member.guild.id,
                resolution.invite.code,
                inviter.id if inviter else "unknown",
            )
        elif resolution.ambiguous:
            logger.info(
                "%s joined %s via one of: %s",
                member.id,
                member.guild.id,
                ", ".join(invite.code for invite in resolution.candidates),
            )
        else:
            logger.warning("Couldn't determine which invite %s used to join %s", member.id, member.guild.id)

        await self._assign_invite_roles(member, resolution)
        await self._send_welcome(member)


async def setup(bot: StarbrightBot) -> None:
    """Entry point discord.py calls to load this cog.
    :param bot: the running bot instance
    """
    await bot.add_cog(Welcomes(bot))

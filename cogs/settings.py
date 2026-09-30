from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.bot import StarbrightBot
from core.settings import SettingDefinition, SettingType, SettingValue


async def _may_edit(
    bot: StarbrightBot, interaction: discord.Interaction, definition: SettingDefinition
) -> bool:
    """Reject the interaction if this setting requires a higher permission tier than the user has.
    :param bot: the running bot instance
    :param interaction: the interaction attempting to change the setting
    :param definition: the setting being changed
    :return: True if the change may proceed
    """
    if (
        definition.min_tier is not None
        and bot.permissions.get_tier(interaction.user) < definition.min_tier
    ):
        await interaction.response.send_message(
            "You don't have permission to change this setting.", ephemeral=True
        )
        return False
    return True


class ValueModal(discord.ui.Modal):
    """Modal for editing a string/integer/float setting's value."""

    def __init__(self, screen: CategoryView, definition: SettingDefinition):
        """
        :param screen: the category screen the change is staged on
        :param definition: the setting being edited
        """
        super().__init__(title=definition.label[:45])
        self.screen = screen
        self.definition = definition
        current = screen.value(definition.key)
        self.value_input = discord.ui.TextInput(
            label=definition.label[:45],
            style=(
                discord.TextStyle.paragraph
                if definition.multiline
                else discord.TextStyle.short
            ),
            placeholder=definition.description[:100],
            default=str(current) if current is not None else None,
            required=not definition.optional,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Validate the submitted value and stage it on the category screen.
        :param interaction: the modal submission interaction
        """
        raw = self.value_input.value.strip()
        if not raw and not self.definition.optional:
            await interaction.response.send_message(
                f"**{self.definition.label}** can't be empty.", ephemeral=True
            )
            return
        try:
            if not raw:
                value: SettingValue = None
            elif self.definition.type is SettingType.INTEGER:
                value = int(raw)
            elif self.definition.type is SettingType.FLOAT:
                value = float(raw)
            else:
                value = raw
        except ValueError:
            await interaction.response.send_message(
                f"`{raw}` is not a valid {self.definition.type.value} value.",
                ephemeral=True,
            )
            return

        if value is not None and self.definition.normalize is not None:
            try:
                value = self.definition.normalize(value)
            except ValueError as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return

        await self.screen.stage(interaction, self.definition, value)


class BackButton(discord.ui.Button):
    """Returns from a category screen to the top-level category picker."""

    def __init__(self, bot: StarbrightBot, disabled: bool):
        """
        :param bot: the running bot instance
        :param disabled: whether to disable it, while there are unsaved changes that leaving would lose
        """
        super().__init__(
            label="◀ Back", style=discord.ButtonStyle.secondary, disabled=disabled
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        """Redraw the category picker in place.
        :param interaction: the button click interaction
        """
        await interaction.response.edit_message(view=HomeView(self.bot))


class SaveButton(discord.ui.Button):
    """Saves every change staged on the category screen."""

    def __init__(self, disabled: bool):
        """
        :param disabled: whether to disable it, when there's nothing to save
        """
        super().__init__(
            label="Save", style=discord.ButtonStyle.success, disabled=disabled
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Persist the staged changes and redraw the category screen.
        :param interaction: the button click interaction
        """
        screen: CategoryView = self.view
        for key in screen.pending:
            if not await _may_edit(screen.bot, interaction, screen.definitions[key]):
                return
        for key, value in screen.pending.items():
            await screen.bot.settings.set(key, value)
        count = len(screen.pending)
        await interaction.response.edit_message(
            view=CategoryView(
                screen.bot,
                screen.category,
                notice=f"✅ Saved {count} change{'s' if count != 1 else ''}.",
            )
        )


class RejectButton(discord.ui.Button):
    """Discards every change staged on the category screen."""

    def __init__(self, disabled: bool):
        """
        :param disabled: whether to disable it, when there's nothing to discard
        """
        super().__init__(
            label="Reject", style=discord.ButtonStyle.danger, disabled=disabled
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Rebuild the category screen with its saved values.
        :param interaction: the button click interaction
        """
        screen: CategoryView = self.view
        await interaction.response.edit_message(
            view=CategoryView(
                screen.bot, screen.category, notice="Discarded unsaved changes."
            )
        )


class PreviewButton(discord.ui.Button):
    """Shows what the category's settings produce, with any unsaved changes applied."""

    def __init__(self):
        super().__init__(label="Preview", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Send the preview as a separate ephemeral message.
        :param interaction: the button click interaction
        """
        screen: CategoryView = self.view
        builder = screen.bot.settings.preview_for(screen.category)
        preview = discord.ui.LayoutView()
        preview.add_item(
            discord.ui.TextDisplay(
                f"-# Preview of {screen.category}"
                + (" with unsaved changes" if screen.pending else "")
            )
        )
        items = builder(interaction.user, screen.value)
        for item in items or [discord.ui.TextDisplay("*Nothing would be shown.*")]:
            preview.add_item(item)
        await interaction.response.send_message(
            view=preview,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ToggleButton(discord.ui.Button):
    """Flips a boolean setting and redraws the category screen with the new state."""

    def __init__(self, definition: SettingDefinition, current: SettingValue):
        """
        :param definition: the boolean setting this button controls
        :param current: the setting's value, including any unsaved change
        """
        super().__init__(
            label="On" if current else "Off",
            style=(
                discord.ButtonStyle.success
                if current
                else discord.ButtonStyle.secondary
            ),
        )
        self.definition = definition
        self.current = bool(current)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Stage the flipped value.
        :param interaction: the button click interaction
        """
        await self.view.stage(interaction, self.definition, not self.current)


class EditButton(discord.ui.Button):
    """Opens a modal to edit a string/integer/float setting."""

    def __init__(self, definition: SettingDefinition):
        """
        :param definition: the setting this button edits
        """
        super().__init__(label="Edit")
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Show the edit modal for this setting.
        :param interaction: the button click interaction
        """
        await interaction.response.send_modal(ValueModal(self.view, self.definition))


class ChoiceSettingSelect(discord.ui.Select):
    """Picks a value for a setting from its fixed list of choices."""

    def __init__(self, definition: SettingDefinition, current: SettingValue):
        """
        :param definition: the CHOICE setting this select controls
        :param current: the setting's value, including any unsaved change
        """
        options = [
            discord.SelectOption(label=choice, default=(choice == current))
            for choice in definition.choices or []
        ]
        super().__init__(
            placeholder=f"Choose a value for {definition.label}...", options=options
        )
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Stage the chosen value.
        :param interaction: the select interaction
        """
        await self.view.stage(interaction, self.definition, self.values[0])


class ChannelSettingSelect(discord.ui.ChannelSelect):
    """Picks a channel for a setting using Discord's native channel picker."""

    def __init__(self, definition: SettingDefinition, current: SettingValue):
        """
        :param definition: the CHANNEL setting this select controls
        :param current: the setting's value, including any unsaved change
        """
        super().__init__(
            placeholder=f"Choose a channel for {definition.label}...",
            min_values=0 if definition.optional else 1,
            default_values=[discord.Object(id=current)] if current else [],
        )
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Stage the chosen channel.
        :param interaction: the select interaction
        """
        value = self.values[0].id if self.values else None
        await self.view.stage(interaction, self.definition, value)


class RoleSettingSelect(discord.ui.RoleSelect):
    """Picks one role, or one or more roles, for a setting using Discord's native role picker."""

    def __init__(self, definition: SettingDefinition, current: SettingValue):
        """
        :param definition: the ROLE or MULTI_ROLE setting this select controls
        :param current: the setting's value, including any unsaved change
        """
        self.is_multi = definition.type is SettingType.MULTI_ROLE
        current_ids = current if self.is_multi else ([current] if current else [])
        super().__init__(
            placeholder=f"Choose {'role(s)' if self.is_multi else 'a role'} for {definition.label}...",
            min_values=0 if self.is_multi or definition.optional else 1,
            max_values=25 if self.is_multi else 1,
            default_values=[discord.Object(id=role_id) for role_id in current_ids],
        )
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Stage the chosen role(s).
        :param interaction: the select interaction
        """
        if self.is_multi:
            value: SettingValue = [role.id for role in self.values]
        else:
            value = self.values[0].id if self.values else None
        await self.view.stage(interaction, self.definition, value)


class UserSettingSelect(discord.ui.UserSelect):
    """Picks one user, or one or more users, for a setting using Discord's native user picker."""

    def __init__(self, definition: SettingDefinition, current: SettingValue):
        """
        :param definition: the USER or MULTI_USER setting this select controls
        :param current: the setting's value, including any unsaved change
        """
        self.is_multi = definition.type is SettingType.MULTI_USER
        current_ids = current if self.is_multi else ([current] if current else [])
        super().__init__(
            placeholder=f"Choose {'user(s)' if self.is_multi else 'a user'} for {definition.label}...",
            min_values=0 if self.is_multi or definition.optional else 1,
            max_values=25 if self.is_multi else 1,
            default_values=[discord.Object(id=user_id) for user_id in current_ids],
        )
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Stage the chosen user(s).
        :param interaction: the select interaction
        """
        if self.is_multi:
            value: SettingValue = [user.id for user in self.values]
        else:
            value = self.values[0].id if self.values else None
        await self.view.stage(interaction, self.definition, value)


_MENTION_SIGILS = {
    SettingType.CHANNEL: "#",
    SettingType.ROLE: "@&",
    SettingType.USER: "@",
    SettingType.MULTI_ROLE: "@&",
    SettingType.MULTI_USER: "@",
}


def _mention(definition: SettingDefinition, value: object) -> str:
    """Render a CHANNEL/ROLE/USER (or MULTI_ROLE/MULTI_USER) setting's current value as mention(s).
    :param definition: the setting being displayed
    :param value: the setting's current value: a snowflake ID, a list of them, or None/empty
    :return: one or more mentions, or an italicized placeholder if nothing is set
    """
    sigil = _MENTION_SIGILS[definition.type]
    ids = value if isinstance(value, list) else ([value] if value is not None else [])
    if not ids:
        return "*Not set*"
    return ", ".join(f"<{sigil}{item_id}>" for item_id in ids)


def _display_value(value: object) -> str:
    """Render a plain setting value for the category screen, shortened to one line.
    :param value: the setting's current value
    :return: the value in inline code, or an italicized placeholder if nothing is set
    """
    if value is None:
        return "*Not set*"
    text = " ".join(str(value).split())
    if len(text) > 100:
        text = text[:99] + "…"
    return f"`{text}`"


def _setting_items(
    definition: SettingDefinition, value: SettingValue, unsaved: bool
) -> list[discord.ui.Item]:
    """Build the display text and control for one setting, matched to its type.
    :param definition: the setting to render
    :param value: the setting's value, including any unsaved change
    :param unsaved: whether the value is an unsaved change
    :return: one or two items: a description TextDisplay, and its control
    """
    marker = " · *unsaved*" if unsaved else ""
    header = f"**{definition.label}**{marker}\n{definition.description}"

    if definition.type is SettingType.BOOLEAN:
        return [
            discord.ui.Section(
                discord.ui.TextDisplay(header),
                accessory=ToggleButton(definition, value),
            )
        ]

    if definition.type in (SettingType.STRING, SettingType.INTEGER, SettingType.FLOAT):
        text = discord.ui.TextDisplay(
            f"{header}\nCurrent value: {_display_value(value)}"
        )
        return [discord.ui.Section(text, accessory=EditButton(definition))]

    if definition.type is SettingType.CHOICE:
        text = discord.ui.TextDisplay(
            f"{header}\nCurrent value: {_display_value(value)}"
        )
        return [text, discord.ui.ActionRow(ChoiceSettingSelect(definition, value))]

    select_cls = {
        SettingType.CHANNEL: ChannelSettingSelect,
        SettingType.ROLE: RoleSettingSelect,
        SettingType.USER: UserSettingSelect,
        SettingType.MULTI_ROLE: RoleSettingSelect,
        SettingType.MULTI_USER: UserSettingSelect,
    }[definition.type]
    text = discord.ui.TextDisplay(f"{header}\nCurrent: {_mention(definition, value)}")
    return [text, discord.ui.ActionRow(select_cls(definition, value))]


class CategoryView(discord.ui.LayoutView):
    """Shows every setting registered under one category, staging edits until Save or Reject is pressed."""

    def __init__(
        self,
        bot: StarbrightBot,
        category: str,
        pending: Optional[dict[str, SettingValue]] = None,
        notice: Optional[str] = None,
    ):
        """
        :param bot: the running bot instance
        :param category: the category to display
        :param pending: unsaved changes carried over from the previous redraw, by setting key
        :param notice: a one-line status message to show under the title
        """
        super().__init__(timeout=180)
        self.bot = bot
        self.category = category
        self.pending = pending or {}
        self.definitions = {d.key: d for d in bot.settings.by_category(category)}

        title = f"## {category} Settings"
        if self.pending:
            count = len(self.pending)
            title += f"\n-# {count} unsaved change{'s' if count != 1 else ''}. Press Save to apply."
        elif notice:
            title += f"\n-# {notice}"

        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(title))
        container.add_item(discord.ui.Separator())
        for index, definition in enumerate(self.definitions.values()):
            if index > 0:
                container.add_item(
                    discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
                )
            for item in _setting_items(
                definition, self.value(definition.key), definition.key in self.pending
            ):
                container.add_item(item)
        container.add_item(discord.ui.Separator())

        controls = discord.ui.ActionRow(
            BackButton(bot, disabled=bool(self.pending)),
            SaveButton(disabled=not self.pending),
            RejectButton(disabled=not self.pending),
        )
        if bot.settings.preview_for(category) is not None:
            controls.add_item(PreviewButton())
        container.add_item(controls)

        try:
            self.add_item(container)
        except ValueError:
            raise ValueError(
                f"category {category!r} has {len(self.definitions)} settings, too many to fit on one "
                "settings screen (Discord caps a message at 40 components) — split it into "
                "two categories"
            ) from None

    def value(self, key: str) -> SettingValue:
        """Return a setting's value as shown on this screen: its unsaved change if it has one, else its saved value.
        :param key: the setting's registered key
        :return: the setting's value
        """
        return self.pending[key] if key in self.pending else self.bot.settings.get(key)

    async def stage(
        self,
        interaction: discord.Interaction,
        definition: SettingDefinition,
        value: SettingValue,
    ) -> None:
        """Record an unsaved change and rebild the screen. Changing a setting back to its saved value unstages it.
        :param interaction: the interaction that made the change, whose message is redrawn
        :param definition: the setting being changed
        :param value: the new value
        """
        if not await _may_edit(self.bot, interaction, definition):
            return
        pending = dict(self.pending)
        if value == self.bot.settings.get(definition.key):
            pending.pop(definition.key, None)
        else:
            pending[definition.key] = value
        await interaction.response.edit_message(
            view=CategoryView(self.bot, self.category, pending)
        )


class CategoryPicker(discord.ui.Select):
    """Top-level dropdown for choosing which cog's settings to view."""

    def __init__(self, bot: StarbrightBot):
        """
        :param bot: the running bot instance
        """
        options = [
            discord.SelectOption(label=category)
            for category in bot.settings.categories()
        ]
        super().__init__(placeholder="Choose a settings category...", options=options)
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        """Show the chosen category's settings.
        :param interaction: the select interaction
        """
        category = self.values[0]
        page = self.bot.settings.page_for(category)
        view = page(interaction) if page else CategoryView(self.bot, category)
        await interaction.response.edit_message(view=view)


class HomeView(discord.ui.LayoutView):
    """Top-level screen: a header and a single dropdown listing every registered category."""

    def __init__(self, bot: StarbrightBot):
        """
        :param bot: the running bot instance
        """
        super().__init__(timeout=180)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                "## ⚙️ Bot Settings\nChoose a category to view or edit its settings."
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(CategoryPicker(bot)),
        )
        self.add_item(container)


class SettingsCog(commands.Cog):
    """Slash command entry point for viewing and editing every cog-registered setting."""

    def __init__(self, bot: StarbrightBot):
        """
        :param bot: the running bot instance
        """
        self.bot = bot

    @app_commands.command(
        name="settings", description="View or change the bot's settings."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def settings(self, interaction: discord.Interaction) -> None:
        """Open the settings menu.
        :param interaction: the command invocation interaction
        """
        if not self.bot.settings.categories():
            await interaction.response.send_message(
                "No settings have been registered yet.", ephemeral=True
            )
            return
        await interaction.response.send_message(view=HomeView(self.bot), ephemeral=True)


async def setup(bot: StarbrightBot) -> None:
    """Entry point discord.py calls to load this cog.
    :param bot: the running bot instance
    """
    await bot.add_cog(SettingsCog(bot))

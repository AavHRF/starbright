from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.bot import StarbrightBot
from core.settings import SettingDefinition, SettingType


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

    def __init__(self, bot: StarbrightBot, definition: SettingDefinition):
        """
        :param bot: the running bot instance
        :param definition: the setting being edited
        """
        super().__init__(title=definition.label[:45])
        self.bot = bot
        self.definition = definition
        current = bot.settings.get(definition.key)
        self.value_input = discord.ui.TextInput(
            label=definition.label[:45],
            default=str(current) if current is not None else None,
            required=True,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Validate and persist the submitted value.
        :param interaction: the modal submission interaction
        """
        if not await _may_edit(self.bot, interaction, self.definition):
            return

        raw = self.value_input.value
        try:
            if self.definition.type is SettingType.INTEGER:
                value: object = int(raw)
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

        await self.bot.settings.set(self.definition.key, value)
        await interaction.response.send_message(
            f"**{self.definition.label}** set to `{value}`.", ephemeral=True
        )


class BackButton(discord.ui.Button):
    """Returns from a category screen to the top-level category picker."""

    def __init__(self, bot: StarbrightBot):
        """
        :param bot: the running bot instance
        """
        super().__init__(label="◀ Back", style=discord.ButtonStyle.secondary)
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        """Redraw the category picker in place.
        :param interaction: the button click interaction
        """
        await interaction.response.edit_message(view=HomeView(self.bot))


class ToggleButton(discord.ui.Button):
    """Flips a boolean setting and redraws the category screen with the new state."""

    def __init__(self, bot: StarbrightBot, definition: SettingDefinition):
        """
        :param bot: the running bot instance
        :param definition: the boolean setting this button controls
        """
        value = bool(bot.settings.get(definition.key))
        super().__init__(
            label="On" if value else "Off",
            style=(
                discord.ButtonStyle.success if value else discord.ButtonStyle.secondary
            ),
        )
        self.bot = bot
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Toggle the setting and redraw the category screen.
        :param interaction: the button click interaction
        """
        if not await _may_edit(self.bot, interaction, self.definition):
            return
        new_value = not bool(self.bot.settings.get(self.definition.key))
        await self.bot.settings.set(self.definition.key, new_value)
        await interaction.response.edit_message(
            view=CategoryView(self.bot, self.definition.category)
        )


class EditButton(discord.ui.Button):
    """Opens a modal to edit a string/integer/float setting."""

    def __init__(self, bot: StarbrightBot, definition: SettingDefinition):
        """
        :param bot: the running bot instance
        :param definition: the setting this button edits
        """
        super().__init__(label="Edit")
        self.bot = bot
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Show the edit modal for this setting.
        :param interaction: the button click interaction
        """
        await interaction.response.send_modal(ValueModal(self.bot, self.definition))


class ChoiceSettingSelect(discord.ui.Select):
    """Picks a value for a setting from its fixed list of choices."""

    def __init__(self, bot: StarbrightBot, definition: SettingDefinition):
        """
        :param bot: the running bot instance
        :param definition: the CHOICE setting this select controls
        """
        current = bot.settings.get(definition.key)
        options = [
            discord.SelectOption(label=choice, default=(choice == current))
            for choice in definition.choices or []
        ]
        super().__init__(
            placeholder=f"Choose a value for {definition.label}...", options=options
        )
        self.bot = bot
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Persist the chosen value and redraw the category screen.
        :param interaction: the select interaction
        """
        if not await _may_edit(self.bot, interaction, self.definition):
            return
        await self.bot.settings.set(self.definition.key, self.values[0])
        await interaction.response.edit_message(
            view=CategoryView(self.bot, self.definition.category)
        )


class ChannelSettingSelect(discord.ui.ChannelSelect):
    """Picks a channel for a setting using Discord's native channel picker."""

    def __init__(self, bot: StarbrightBot, definition: SettingDefinition):
        """
        :param bot: the running bot instance
        :param definition: the CHANNEL setting this select controls
        """
        current = bot.settings.get(definition.key)
        super().__init__(
            placeholder=f"Choose a channel for {definition.label}...",
            default_values=[discord.Object(id=current)] if current else [],
        )
        self.bot = bot
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Persist the chosen channel and redraw the category screen.
        :param interaction: the select interaction
        """
        if not await _may_edit(self.bot, interaction, self.definition):
            return
        await self.bot.settings.set(self.definition.key, self.values[0].id)
        await interaction.response.edit_message(
            view=CategoryView(self.bot, self.definition.category)
        )


class RoleSettingSelect(discord.ui.RoleSelect):
    """Picks one role, or one or more roles, for a setting using Discord's native role picker."""

    def __init__(self, bot: StarbrightBot, definition: SettingDefinition):
        """
        :param bot: the running bot instance
        :param definition: the ROLE or MULTI_ROLE setting this select controls
        """
        self.is_multi = definition.type is SettingType.MULTI_ROLE
        current = bot.settings.get(definition.key)
        current_ids = current if self.is_multi else ([current] if current else [])
        super().__init__(
            placeholder=f"Choose {'role(s)' if self.is_multi else 'a role'} for {definition.label}...",
            min_values=0 if self.is_multi else 1,
            max_values=25 if self.is_multi else 1,
            default_values=[discord.Object(id=role_id) for role_id in current_ids],
        )
        self.bot = bot
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Persist the chosen role(s) and redraw the category screen.
        :param interaction: the select interaction
        """
        if not await _may_edit(self.bot, interaction, self.definition):
            return
        value = (
            [role.id for role in self.values] if self.is_multi else self.values[0].id
        )
        await self.bot.settings.set(self.definition.key, value)
        await interaction.response.edit_message(
            view=CategoryView(self.bot, self.definition.category)
        )


class UserSettingSelect(discord.ui.UserSelect):
    """Picks one user, or one or more users, for a setting using Discord's native user picker."""

    def __init__(self, bot: StarbrightBot, definition: SettingDefinition):
        """
        :param bot: the running bot instance
        :param definition: the USER or MULTI_USER setting this select controls
        """
        self.is_multi = definition.type is SettingType.MULTI_USER
        current = bot.settings.get(definition.key)
        current_ids = current if self.is_multi else ([current] if current else [])
        super().__init__(
            placeholder=f"Choose {'user(s)' if self.is_multi else 'a user'} for {definition.label}...",
            min_values=0 if self.is_multi else 1,
            max_values=25 if self.is_multi else 1,
            default_values=[discord.Object(id=user_id) for user_id in current_ids],
        )
        self.bot = bot
        self.definition = definition

    async def callback(self, interaction: discord.Interaction) -> None:
        """Persist the chosen user(s) and redraw the category screen.
        :param interaction: the select interaction
        """
        if not await _may_edit(self.bot, interaction, self.definition):
            return
        value = (
            [user.id for user in self.values] if self.is_multi else self.values[0].id
        )
        await self.bot.settings.set(self.definition.key, value)
        await interaction.response.edit_message(
            view=CategoryView(self.bot, self.definition.category)
        )


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


def _setting_items(
    bot: StarbrightBot, definition: SettingDefinition
) -> list[discord.ui.Item]:
    """Build the display text and control for one setting, matched to its type.
    :param bot: the running bot instance
    :param definition: the setting to render
    :return: one or two items: a description TextDisplay, and its control
    """
    value = bot.settings.get(definition.key)
    header = f"**{definition.label}**\n{definition.description}"

    if definition.type is SettingType.BOOLEAN:
        return [
            discord.ui.Section(
                discord.ui.TextDisplay(header), accessory=ToggleButton(bot, definition)
            )
        ]

    if definition.type in (SettingType.STRING, SettingType.INTEGER, SettingType.FLOAT):
        text = discord.ui.TextDisplay(f"{header}\nCurrent value: `{value}`")
        return [discord.ui.Section(text, accessory=EditButton(bot, definition))]

    if definition.type is SettingType.CHOICE:
        text = discord.ui.TextDisplay(f"{header}\nCurrent value: `{value}`")
        return [text, discord.ui.ActionRow(ChoiceSettingSelect(bot, definition))]

    select_cls = {
        SettingType.CHANNEL: ChannelSettingSelect,
        SettingType.ROLE: RoleSettingSelect,
        SettingType.USER: UserSettingSelect,
        SettingType.MULTI_ROLE: RoleSettingSelect,
        SettingType.MULTI_USER: UserSettingSelect,
    }[definition.type]
    text = discord.ui.TextDisplay(f"{header}\nCurrent: {_mention(definition, value)}")
    return [text, discord.ui.ActionRow(select_cls(bot, definition))]


class CategoryView(discord.ui.LayoutView):
    """Shows every setting registered under one category, each with its matching control."""

    def __init__(self, bot: StarbrightBot, category: str):
        """
        :param bot: the running bot instance
        :param category: the category to display
        """
        super().__init__(timeout=180)
        definitions = bot.settings.by_category(category)

        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(f"## {category} Settings"))
        container.add_item(discord.ui.Separator())
        for index, definition in enumerate(definitions):
            if index > 0:
                container.add_item(
                    discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
                )
            for item in _setting_items(bot, definition):
                container.add_item(item)
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(BackButton(bot)))

        try:
            self.add_item(container)
        except ValueError:
            raise ValueError(
                f"category {category!r} has {len(definitions)} settings, too many to fit on one "
                "settings screen (Discord caps a message at 40 components) — split it into "
                "two categories"
            ) from None


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
        await interaction.response.edit_message(
            view=CategoryView(self.bot, self.values[0])
        )


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

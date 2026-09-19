from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Collection, Optional

import discord

from core.actions import ActionContext, ParameterKind
from core.permissions import PermissionTier

SENTINEL_NAMES = {"confirm", "approve"}
TOKEN_RE = re.compile(r"^\$([ucrs]\d+)$")
_MENTION_RE = re.compile(r"^<[@#](?:[!&])?(\d+)>$")
_TOKEN_KIND = {"u": "user", "c": "channel", "r": "role", "s": "string"}
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass
class ChainStep:
    """One parsed step in a command chain: a command name plus its raw or already-resolved args."""

    name: str
    args: dict[str, Any]


def _split_segments(text: str) -> list[list[str]]:
    """Tokenize a chain string shell-style and split it into per-step token lists on unquoted pipes.
    :param text: the full chain string
    :return: one token list per step
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars="|")
    lexer.whitespace_split = True

    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) == {"|"}:
            # A run of N pipes ends the current step and opens N-1 empty ones, which are dropped below.
            segments.extend([] for _ in token)
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def parse_chain(text: str) -> list[ChainStep]:
    """Parse a pipe-delimited chain string into steps, each a command name plus its key:value args.
    Pipes inside quotes are part of the value, not step separators.
    :param text: the chain string, e.g. 'confirm:"Sure?" | kick member:$u1 reason:"spam"'
    :return: the parsed steps, in order
    """
    steps = []
    for tokens in _split_segments(text):
        first, *rest = tokens
        if ":" in first:
            name, default_value = first.split(":", 1)
            args: dict[str, Any] = {"$default": default_value}
        else:
            name, args = first, {}

        for token in rest:
            if ":" not in token:
                raise ValueError(f"expected key:value in step {name!r}, got {token!r}")
            key, value = token.split(":", 1)
            args[key] = value

        steps.append(ChainStep(name=name, args=args))
    return steps


def find_placeholders(steps: list[ChainStep]) -> set[str]:
    """List every distinct $u1/$c2/$r1/$s3-style placeholder token used across a chain's steps.
    :param steps: parsed chain steps
    :return: the set of placeholder tokens found, e.g. {"$u1", "$s1"}
    """
    return {
        value
        for step in steps
        for value in step.args.values()
        if TOKEN_RE.match(str(value))
    }


def substitute_tokens(
    steps: list[ChainStep], bindings: dict[str, Any]
) -> list[ChainStep]:
    """Replace exact-match placeholder tokens in every step's args with their bound values.
    :param steps: parsed chain steps (not modified)
    :param bindings: placeholder token (e.g. "$u1") -> resolved value
    :return: new steps with placeholders replaced
    """
    return [
        ChainStep(
            name=step.name,
            args={key: bindings.get(value, value) for key, value in step.args.items()},
        )
        for step in steps
    ]


def coerce_value(raw: Any, kind: ParameterKind, guild: discord.Guild) -> Any:
    """Coerce a raw chain-string value (or pass through an already-resolved object) to an action's expected type.
    :param raw: a string from chain text, or an already-resolved object from alias token substitution
    :param kind: the action parameter's declared kind
    :param guild: the guild to resolve mentions/IDs against
    :return: the coerced value
    """
    if not isinstance(raw, str):
        return raw

    if kind == "string":
        return raw
    if kind == "integer":
        return int(raw)
    if kind == "boolean":
        return raw.lower() in ("true", "yes", "1", "on")

    match = _MENTION_RE.match(raw)
    snowflake = int(match.group(1)) if match else (int(raw) if raw.isdigit() else None)
    if snowflake is None:
        raise ValueError(f"could not resolve a {kind} from {raw!r}")

    resolved = {
        "user": guild.get_member,
        "channel": guild.get_channel,
        "role": guild.get_role,
    }[kind](snowflake)
    if resolved is None:
        raise ValueError(f"no {kind} with ID {snowflake} in this server")
    return resolved


def approve_tier(step: ChainStep) -> PermissionTier:
    """Resolve the tier an 'approve' sentinel requires of its approvers.
    :param step: the approve sentinel step, optionally naming a tier in its default value
    :return: the named tier, or MODERATOR if none was given
    :raises ValueError: if the name isn't a permission tier
    """
    name = str(step.args.get("$default", PermissionTier.MODERATOR.name)).upper()
    try:
        return PermissionTier[name]
    except KeyError:
        options = ", ".join(tier.name.lower() for tier in PermissionTier)
        raise ValueError(
            f"'{name.lower()}' is not a permission tier for approve; use one of: {options}"
        ) from None


def validate_chain(
    bot: Any, steps: list[ChainStep], *, allowed_placeholders: Collection[str] = ()
) -> None:
    """Validate a chain's command names, sentinels, and placeholders before running or storing it.
    :param bot: the running bot instance
    :param steps: parsed chain steps (pre-substitution)
    :param allowed_placeholders: the placeholder tokens the caller will be able to bind; any other is rejected
    :raises ValueError: if a command is unknown or misplaced, an approve tier is invalid, or a placeholder
        is unavailable or its type doesn't match its parameter
    """
    for value in sorted(find_placeholders(steps)):
        if value not in allowed_placeholders:
            hint = (
                f"; available here: {', '.join(sorted(allowed_placeholders))}"
                if allowed_placeholders
                else ""
            )
            raise ValueError(
                f"placeholder '{value}' can't be bound in this context{hint}"
            )

    remaining = steps
    while remaining and remaining[0].name in SENTINEL_NAMES:
        sentinel, remaining = remaining[0], remaining[1:]
        if sentinel.name == "approve":
            approve_tier(sentinel)

    for step in remaining:
        if step.name in SENTINEL_NAMES:
            raise ValueError(f"'{step.name}' must be at the very start of the chain")
        spec = bot.actions.get(step.name)
        if spec is None:
            raise ValueError(f"unknown command in chain: '{step.name}'")

        params_by_name = {p.name: p for p in spec.parameters}
        for key, value in step.args.items():
            match = TOKEN_RE.match(str(value))
            if not match or key not in params_by_name:
                continue
            expected_kind = _TOKEN_KIND[match.group(1)[0]]
            if params_by_name[key].kind != expected_kind:
                raise ValueError(
                    f"'{value}' is a {expected_kind} placeholder but "
                    f"'{step.name}.{key}' expects {params_by_name[key].kind}"
                )


def inherited_min_tier(bot: Any, steps: list[ChainStep]) -> PermissionTier:
    """Compute the highest tier any non-sentinel step in a chain inherently requires.
    :param bot: the running bot instance
    :param steps: parsed chain steps
    :return: the highest min_tier among the chain's actions, or USER if it has none
    """
    remaining = steps
    while remaining and remaining[0].name in SENTINEL_NAMES:
        remaining = remaining[1:]
    tiers = [
        spec.min_tier
        for step in remaining
        if (spec := bot.actions.get(step.name)) is not None
    ]
    return max(tiers, default=PermissionTier.USER)


def parse_duration(text: str) -> timedelta:
    """Parse a simple duration string like '30s', '10m', '2h', or '1d' into a timedelta.
    :param text: the duration string
    :return: the parsed timedelta
    """
    match = _DURATION_RE.match(text.strip().lower())
    if not match:
        raise ValueError(
            f"invalid duration {text!r}; use e.g. '30s', '10m', '2h', '1d'"
        )
    amount, unit = match.groups()
    if int(amount) == 0:
        raise ValueError("duration must be greater than zero")
    return timedelta(seconds=int(amount) * _DURATION_UNITS[unit])


class ConfirmView(discord.ui.View):
    """A Confirm/Cancel button pair, gating whether the rest of a chain should run."""

    def __init__(self, *, allowed_user_id: int, timeout: float = 120.0):
        """
        :param allowed_user_id: the only user allowed to answer this prompt
        :param timeout: seconds to wait before the prompt expires
        """
        super().__init__(timeout=timeout)
        self.allowed_user_id = allowed_user_id
        self.confirmed: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Reject button clicks from anyone but the original requester.
        :param interaction: the button click interaction
        :return: True if the click may proceed
        """
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "This isn't your confirmation to answer.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Record the confirmation and stop waiting."""
        self.confirmed = True
        await interaction.response.edit_message(content="Confirmed.", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Record the cancellation and stop waiting."""
        self.confirmed = False
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class ApproveView(discord.ui.View):
    """An Approve/Deny button pair that only a qualifying, different user may click."""

    def __init__(
        self,
        bot: Any,
        *,
        requester_id: int,
        required_tier: PermissionTier,
        timeout: float = 600.0,
    ):
        """
        :param bot: the running bot instance
        :param requester_id: the user who requested this chain; may not approve their own request
        :param required_tier: the minimum tier a clicker must have to approve or deny
        :param timeout: seconds to wait before the request expires
        """
        super().__init__(timeout=timeout)
        self.bot = bot
        self.requester_id = requester_id
        self.required_tier = required_tier
        self.approved: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Reject clicks from the requester themselves or anyone below the required tier.
        :param interaction: the button click interaction
        :return: True if the click may proceed
        """
        if interaction.user.id == self.requester_id:
            await interaction.response.send_message(
                "You can't approve your own request.", ephemeral=True
            )
            return False
        if not self.bot.permissions.has_tier(interaction.user, self.required_tier):
            await interaction.response.send_message(
                f"You need {self.required_tier.name.title()} or higher to approve this.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Record the approval and stop waiting."""
        self.approved = True
        await interaction.response.edit_message(
            content=f"Approved by {interaction.user.mention}.", view=None
        )
        self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Record the denial and stop waiting."""
        self.approved = False
        await interaction.response.edit_message(
            content=f"Denied by {interaction.user.mention}.", view=None
        )
        self.stop()


async def _run_confirm(ctx: ActionContext, step: ChainStep) -> bool:
    """Show a confirm/cancel prompt to the requester and wait for their answer.
    :param ctx: the chain's execution context
    :param step: the confirm sentinel step, optionally carrying a prompt in its default value
    :return: True if confirmed, False if cancelled or timed out
    """
    prompt = step.args.get("$default", "Are you sure?")
    view = ConfirmView(allowed_user_id=ctx.user.id)
    await ctx.reply(prompt, view=view, ephemeral=True)
    await view.wait()
    return bool(view.confirmed)


async def _run_approve(ctx: ActionContext, step: ChainStep) -> bool:
    """Post an approval request visible to qualifying approvers and wait for their answer.
    :param ctx: the chain's execution context
    :param step: the approve sentinel step, optionally naming a required tier in its default value
    :return: True if approved, False if denied or timed out
    """
    try:
        required_tier = approve_tier(step)
    except ValueError as exc:
        await ctx.reply(f"Error in `approve`: {exc}", ephemeral=True)
        return False
    view = ApproveView(ctx.bot, requester_id=ctx.user.id, required_tier=required_tier)
    await ctx.reply(
        f"{ctx.user.mention} wants to run a command chain and needs {required_tier.name.title()} approval.",
        view=view,
    )
    await view.wait()
    return bool(view.approved)


async def execute_chain(
    ctx: ActionContext,
    steps: list[ChainStep],
    *,
    overall_min_tier: Optional[PermissionTier] = None,
) -> None:
    """Run a parsed chain of steps against the bot's action registry, honoring any leading confirm/approve gates.
    :param ctx: the chain's execution context
    :param steps: the parsed (and, for aliases, token-substituted) steps to run
    :param overall_min_tier: if set, checked once up front in place of each step's own min_tier
    """
    if not steps:
        return

    if overall_min_tier is not None and not ctx.bot.permissions.has_tier(
        ctx.user, overall_min_tier
    ):
        await ctx.reply(
            f"You need {overall_min_tier.name.title()} or higher to run this.",
            ephemeral=True,
        )
        return

    remaining = steps
    while remaining and remaining[0].name in SENTINEL_NAMES:
        sentinel, remaining = remaining[0], remaining[1:]
        runner = _run_confirm if sentinel.name == "confirm" else _run_approve
        if not await runner(ctx, sentinel):
            return

    for step in remaining:
        if step.name in SENTINEL_NAMES:
            await ctx.reply(
                f"`{step.name}` must be at the very start of the chain.", ephemeral=True
            )
            return

        spec = ctx.bot.actions.get(step.name)
        if spec is None:
            await ctx.reply(f"Unknown command in chain: `{step.name}`.", ephemeral=True)
            return

        if overall_min_tier is None and not ctx.bot.permissions.has_tier(
            ctx.user, spec.min_tier
        ):
            await ctx.reply(
                f"You need {spec.min_tier.name.title()} or higher to run `{step.name}`.",
                ephemeral=True,
            )
            return

        try:
            kwargs = {
                param.name: coerce_value(step.args[param.name], param.kind, ctx.guild)
                for param in spec.parameters
                if param.name in step.args
            }
        except ValueError as exc:
            await ctx.reply(f"Error in `{step.name}`: {exc}", ephemeral=True)
            return

        missing = [
            p.name for p in spec.parameters if p.required and p.name not in kwargs
        ]
        if missing:
            await ctx.reply(
                f"`{step.name}` is missing required argument(s): {', '.join(missing)}.",
                ephemeral=True,
            )
            return

        await spec.handler(ctx, **kwargs)

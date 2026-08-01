"""KvK scheduler cog: event lifecycle and self-service signup."""
import contextlib
import os
import sqlite3
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from . import kvk_data as kvkdb
from .kvk_util import POSITION_TYPES, generate_time_slots
from .permission_handler import PermissionManager

DB_PATH = "db/kvk.sqlite"
DATE_FMT = "%Y-%m-%d"
DT_FMT = "%Y-%m-%d %H:%M"
VIEW_TIMEOUT = 7200


class KvkScheduling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        os.makedirs("db", exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
        kvkdb.init_schema(self.conn)

    async def cog_unload(self):
        with contextlib.suppress(Exception):
            self.conn.close()

    def _is_global_admin(self, interaction: discord.Interaction) -> bool:
        is_admin, is_global = PermissionManager.is_admin(interaction.user.id)
        return bool(is_admin and is_global)

    @app_commands.command(name="kvk_create", description="Start the KvK event setup wizard (Global Admin only).")
    async def kvk_create(self, interaction: discord.Interaction):
        if not self._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server, not a DM.", ephemeral=True)
            return
        await interaction.response.send_modal(_KvkCreateModal(self))


class _KvkDraft:
    """Holds wizard state between the create modal and the final confirm."""

    def __init__(self, guild_id: int, created_by: int, name: str, event_date: str, scope: str,
                 slots_per_alliance: int | None, signup_open_at: str, signup_close_at: str):
        self.guild_id = guild_id
        self.created_by = created_by
        self.name = name
        self.event_date = event_date
        self.scope = scope
        self.slots_per_alliance = slots_per_alliance
        self.signup_open_at = signup_open_at
        self.signup_close_at = signup_close_at
        self.slot_mode = 0
        self.active_types: list[str] = []
        self.publish_channel_id: int | None = None


class _KvkCreateModal(discord.ui.Modal, title="Create KvK Event"):
    """First step: name, dates, and the alliance-vs-kingdom scope switch."""

    def __init__(self, cog: KvkScheduling):
        super().__init__()
        self.cog = cog
        self.name_input = discord.ui.TextInput(label="Event name", max_length=100)
        self.add_item(self.name_input)
        self.event_date_input = discord.ui.TextInput(
            label="Event date (YYYY-MM-DD)", placeholder="2026-09-01", max_length=10)
        self.add_item(self.event_date_input)
        self.signup_open_input = discord.ui.TextInput(
            label="Signup opens (YYYY-MM-DD HH:MM UTC)", placeholder="2026-08-20 00:00", max_length=16)
        self.add_item(self.signup_open_input)
        self.signup_close_input = discord.ui.TextInput(
            label="Signup closes (YYYY-MM-DD HH:MM UTC)", placeholder="2026-08-31 00:00", max_length=16)
        self.add_item(self.signup_close_input)
        self.slots_per_alliance_input = discord.ui.TextInput(
            label="Slots per alliance (blank = kingdom scope)", required=False, max_length=5)
        self.add_item(self.slots_per_alliance_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        if not name:
            await interaction.response.send_message("Event name is required.", ephemeral=True)
            return

        try:
            event_date = datetime.strptime(self.event_date_input.value.strip(), DATE_FMT).strftime(DATE_FMT)
        except ValueError:
            await interaction.response.send_message("Event date must use format YYYY-MM-DD.", ephemeral=True)
            return

        try:
            signup_open_at = datetime.strptime(self.signup_open_input.value.strip(), DT_FMT).strftime(DT_FMT)
        except ValueError:
            await interaction.response.send_message(
                "Signup open time must use format YYYY-MM-DD HH:MM.", ephemeral=True)
            return

        try:
            signup_close_at = datetime.strptime(self.signup_close_input.value.strip(), DT_FMT).strftime(DT_FMT)
        except ValueError:
            await interaction.response.send_message(
                "Signup close time must use format YYYY-MM-DD HH:MM.", ephemeral=True)
            return

        raw_n = self.slots_per_alliance_input.value.strip()
        if raw_n:
            try:
                slots_per_alliance = int(raw_n)
            except ValueError:
                await interaction.response.send_message(
                    "Slots per alliance must be a whole number.", ephemeral=True)
                return
            if slots_per_alliance <= 0:
                await interaction.response.send_message(
                    "Slots per alliance must be a positive number.", ephemeral=True)
                return
            scope = "alliance"
        else:
            slots_per_alliance = None
            scope = "kingdom"

        draft = _KvkDraft(
            guild_id=interaction.guild_id,
            created_by=interaction.user.id,
            name=name,
            event_date=event_date,
            scope=scope,
            slots_per_alliance=slots_per_alliance,
            signup_open_at=signup_open_at,
            signup_close_at=signup_close_at,
        )

        view = _KvkWizardView(self.cog, draft)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


class _SlotModeSelect(discord.ui.Select):
    def __init__(self, wizard_view: "_KvkWizardView"):
        options = [
            discord.SelectOption(
                label=f"Mode 0 - {len(generate_time_slots(0))} slots", value="0", default=True),
            discord.SelectOption(
                label=f"Mode 1 - {len(generate_time_slots(1))} slots", value="1"),
        ]
        super().__init__(placeholder="Pick the slot grid mode", options=options, min_values=1, max_values=1)
        self.wizard_view = wizard_view

    async def callback(self, interaction: discord.Interaction):
        self.wizard_view.draft.slot_mode = int(self.values[0])
        await self.wizard_view.refresh(interaction)


class _TypesSelect(discord.ui.Select):
    def __init__(self, wizard_view: "_KvkWizardView"):
        options = [discord.SelectOption(label=t, value=t) for t in POSITION_TYPES]
        super().__init__(
            placeholder="Pick active position types", options=options,
            min_values=1, max_values=len(POSITION_TYPES))
        self.wizard_view = wizard_view

    async def callback(self, interaction: discord.Interaction):
        self.wizard_view.draft.active_types = list(self.values)
        await self.wizard_view.refresh(interaction)


class _PublishChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wizard_view: "_KvkWizardView"):
        super().__init__(
            placeholder="Pick the publish channel",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1, max_values=1)
        self.wizard_view = wizard_view

    async def callback(self, interaction: discord.Interaction):
        self.wizard_view.draft.publish_channel_id = self.values[0].id
        await self.wizard_view.refresh(interaction)


class _KvkWizardView(discord.ui.View):
    """Second step: slot grid mode, active types, and the publish channel."""

    def __init__(self, cog: KvkScheduling, draft: _KvkDraft):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.draft = draft
        self.add_item(_SlotModeSelect(self))
        self.add_item(_TypesSelect(self))
        self.add_item(_PublishChannelSelect(self))
        confirm_button = discord.ui.Button(
            label="Confirm and create", style=discord.ButtonStyle.primary, row=3)
        confirm_button.callback = self.confirm
        self.add_item(confirm_button)

    def build_embed(self) -> discord.Embed:
        d = self.draft
        types_text = ", ".join(d.active_types) if d.active_types else "(pick at least one)"
        channel_text = f"<#{d.publish_channel_id}>" if d.publish_channel_id else "(pick a channel)"
        n_text = str(d.slots_per_alliance) if d.scope == "alliance" else "n/a (kingdom scope)"
        embed = discord.Embed(title="Create KvK Event", color=discord.Color.blue())
        embed.add_field(name="Name", value=d.name, inline=False)
        embed.add_field(name="Event date", value=d.event_date, inline=True)
        embed.add_field(name="Scope", value=d.scope, inline=True)
        embed.add_field(name="Slots per alliance", value=n_text, inline=True)
        embed.add_field(name="Slot mode", value=str(d.slot_mode), inline=True)
        embed.add_field(name="Signup window (UTC)", value=f"{d.signup_open_at} to {d.signup_close_at}", inline=False)
        embed.add_field(name="Active types", value=types_text, inline=False)
        embed.add_field(name="Publish channel", value=channel_text, inline=False)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def confirm(self, interaction: discord.Interaction):
        d = self.draft
        if not d.active_types:
            await interaction.response.send_message("Pick at least one active type.", ephemeral=True)
            return
        if d.publish_channel_id is None:
            await interaction.response.send_message("Pick a publish channel.", ephemeral=True)
            return
        if d.scope == "alliance":
            max_slots = len(generate_time_slots(d.slot_mode))
            if d.slots_per_alliance > max_slots:
                await interaction.response.send_message(
                    f"Slots per alliance ({d.slots_per_alliance}) is more than "
                    f"the grid size ({max_slots}).", ephemeral=True)
                return
        await interaction.response.send_modal(_KvkTypeDatesModal(self.cog, self))


class _KvkTypeDatesModal(discord.ui.Modal, title="Set Position Type Dates"):
    """Final step: one date per active position type, then create the event."""

    def __init__(self, cog: KvkScheduling, wizard_view: _KvkWizardView):
        super().__init__()
        self.cog = cog
        self.wizard_view = wizard_view
        self.date_inputs: dict[str, discord.ui.TextInput] = {}
        for position_type in wizard_view.draft.active_types:
            text_input = discord.ui.TextInput(
                label=f"{position_type} date (YYYY-MM-DD)",
                default=wizard_view.draft.event_date,
                max_length=10,
            )
            self.date_inputs[position_type] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        type_dates: list[tuple[str, str]] = []
        for position_type, text_input in self.date_inputs.items():
            try:
                type_date = datetime.strptime(text_input.value.strip(), DATE_FMT).strftime(DATE_FMT)
            except ValueError:
                await interaction.response.send_message(
                    f"{position_type} date must use format YYYY-MM-DD.", ephemeral=True)
                return
            type_dates.append((position_type, type_date))

        draft = self.wizard_view.draft
        event_id = kvkdb.create_event(
            self.cog.conn,
            guild_id=draft.guild_id,
            name=draft.name,
            event_date=draft.event_date,
            scope=draft.scope,
            slots_per_alliance=draft.slots_per_alliance,
            slot_mode=draft.slot_mode,
            signup_open_at=draft.signup_open_at,
            signup_close_at=draft.signup_close_at,
            publish_channel_id=draft.publish_channel_id,
            created_by=draft.created_by,
            created_at=datetime.now(UTC).strftime(DT_FMT),
        )
        kvkdb.set_event_types(self.cog.conn, event_id, type_dates)

        posted = await _post_announcement(interaction.client, draft, event_id, type_dates)

        result_embed = discord.Embed(title="KvK event created", color=discord.Color.green())
        result_embed.add_field(name="Event ID", value=str(event_id), inline=True)
        result_embed.add_field(name="Name", value=draft.name, inline=True)
        if not posted:
            result_embed.add_field(
                name="Announcement",
                value=(
                    "The bot could not post to the publish channel. "
                    f"Run `/kvk_signup event_id:{event_id}` to check channel access."
                ),
                inline=False,
            )

        await interaction.response.edit_message(embed=result_embed, view=None)


async def _post_announcement(
    client: discord.Client, draft: _KvkDraft, event_id: int, type_dates: list[tuple[str, str]]
) -> bool:
    """Post the event announcement to the publish channel. Returns False on failure."""
    channel = client.get_channel(draft.publish_channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(draft.publish_channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    types_text = "\n".join(f"{position_type}: {type_date}" for position_type, type_date in type_dates)
    embed = discord.Embed(
        title=f"KvK Event: {draft.name}",
        description=f"Run `/kvk_signup event_id:{event_id}` to sign up.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Event ID", value=str(event_id), inline=True)
    embed.add_field(name="Event date", value=draft.event_date, inline=True)
    embed.add_field(name="Scope", value=draft.scope, inline=True)
    embed.add_field(
        name="Signup window (UTC)", value=f"{draft.signup_open_at} to {draft.signup_close_at}", inline=False)
    embed.add_field(name="Active types", value=types_text, inline=False)

    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        return False
    return True


async def setup(bot):
    await bot.add_cog(KvkScheduling(bot))

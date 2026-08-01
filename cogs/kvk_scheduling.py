"""KvK scheduler cog: event lifecycle and self-service signup."""
import contextlib
import os
import sqlite3
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from . import kvk_data as kvkdb
from .kvk_util import (
    POSITION_TYPES,
    TROOP_LEVELS,
    compute_training_points,
    format_speedups,
    generate_time_slots,
    parse_desired_slots,
    parse_speedups,
    parse_troop_count,
    troop_tier,
    type_dates_for,
)
from .permission_handler import PermissionManager
from .pimp_my_bot import check_interaction_user, safe_edit_message, theme

DB_PATH = "db/kvk.sqlite"
DATE_FMT = "%Y-%m-%d"
DT_FMT = "%Y-%m-%d %H:%M"
VIEW_TIMEOUT = 7200
_MAX_EVENT_OPTIONS = 25  # Discord caps a select at 25 options
# What each slot grid means, shown in the wizard so "Mode 0 / Mode 1" is not opaque.
# Mode 1 keeps a leading 00:00 stub (covers the 15-min carry-over), then runs on :15/:45.
_SLOT_MODE_HINT = {0: "slots at :00 and :30", 1: "slots at :15 and :45, plus 00:00"}
# Status marker shown next to an event in the pickers.
_STATUS_ICON = {"collecting": theme.editListIcon, "assigned": theme.chartIcon, "published": theme.announceIcon}


def _status_icon(status):
    return _STATUS_ICON.get(status)


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

    def _fids_for_discord(self, discord_id: int) -> list[int]:
        users = sqlite3.connect("db/users.sqlite")
        try:
            rows = users.execute(
                "SELECT fid FROM users WHERE discord_id = ? ORDER BY fid", (discord_id,)).fetchall()
            return [r[0] for r in rows]
        finally:
            users.close()

    async def launch_create(self, interaction: discord.Interaction) -> None:
        """Open the create wizard. Shared by /kvk_create and the /settings KvK menu."""
        if not self._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server, not a DM.", ephemeral=True)
            return
        await interaction.response.send_modal(_KvkCreateModal(self))

    @app_commands.command(name="kvk_create", description="Start the KvK event setup wizard (Global Admin only).")
    async def kvk_create(self, interaction: discord.Interaction):
        await self.launch_create(interaction)

    async def show_kvk_menu(self, interaction: discord.Interaction):
        """Entry point for the /settings KvK Scheduling button (Global Admin only)."""
        if not self._is_global_admin(interaction):
            await interaction.response.send_message(
                f"{theme.deniedIcon} KvK Scheduling is Global Admin only.", ephemeral=True)
            return
        view = _KvkMenuView(self, interaction.guild_id, interaction.user.id)
        await safe_edit_message(interaction, embed=view.build_embed(), view=view, content=None)

    @app_commands.command(name="kvk_signup", description="Sign up for the open KvK event (any registered player).")
    async def kvk_signup(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this in a server, not a DM.", ephemeral=True)
            return
        fids = self._fids_for_discord(interaction.user.id)
        if not fids:
            await interaction.response.send_message("No linked fid found. Run /register first.", ephemeral=True)
            return
        now = datetime.now(UTC).strftime(DT_FMT)
        open_events = kvkdb.list_open_events(self.conn, interaction.guild_id, now)
        if not open_events:
            await interaction.response.send_message(
                "No KvK event is open for signup right now.", ephemeral=True)
            return
        if len(open_events) == 1:
            await self._pick_fid_then_signup(interaction, open_events[0]["id"], fids)
            return
        view = _EventSignupSelectView(self, open_events, fids)
        await interaction.response.send_message(
            "Pick the KvK event to sign up for:", view=view, ephemeral=True)

    async def _pick_fid_then_signup(
        self, interaction: discord.Interaction, event_id: int, fids: list[int], *, edit: bool = False
    ) -> None:
        """Resolve which fid signs up (auto if one, picker if many), then start the signup."""
        if len(fids) == 1:
            await self._start_signup(interaction, event_id, fids[0], edit=edit)
            return
        view = _FidSelectView(self, event_id, fids)
        await self._send_or_edit(
            interaction, "You have more than one linked fid. Pick one:", view=view, edit=edit)

    @app_commands.command(name="kvk_edit_signup", description="Edit a player's KvK signup (Global Admin only).")
    @app_commands.describe(event_id="The KvK event ID", fid="The player's in-game fid")
    async def kvk_edit_signup(self, interaction: discord.Interaction, event_id: int, fid: int):
        if not self._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        await self._start_signup(interaction, event_id, fid, admin_override=True)

    async def _start_signup(
        self, interaction: discord.Interaction, event_id: int, fid: int, *,
        edit: bool = False, admin_override: bool = False,
    ) -> None:
        """Validate the event and signup window, then show the position-type picker.

        admin_override skips the status and signup-window checks, so an admin can
        repair a signup after /kvk_report has moved the event past "collecting".
        """
        ev = kvkdb.get_event(self.conn, event_id)
        if ev is None:
            await self._send_or_edit(interaction, f"Event {event_id} is not open for signup.", edit=edit)
            return

        if not admin_override:
            if ev["status"] != "collecting":
                await self._send_or_edit(interaction, f"Event {event_id} is not open for signup.", edit=edit)
                return

            now = datetime.now(UTC).strftime(DT_FMT)
            if not (ev["signup_open_at"] <= now <= ev["signup_close_at"]):
                await self._send_or_edit(interaction, "The signup window for this event is closed.", edit=edit)
                return

        active_types = kvkdb.get_active_types(self.conn, event_id)
        if not active_types:
            await self._send_or_edit(interaction, f"Event {event_id} has no active position types.", edit=edit)
            return

        view = _SignupTypesView(self, event_id, fid, active_types)
        await self._send_or_edit(
            interaction, "Pick position types to sign up for (up to 3), then press Enter speedups.",
            view=view, edit=edit)

    @staticmethod
    async def _send_or_edit(
        interaction: discord.Interaction, content: str, *, view: discord.ui.View | None = None, edit: bool = False
    ) -> None:
        if edit:
            await interaction.response.edit_message(content=content, view=view)
        else:
            await interaction.response.send_message(content, view=view, ephemeral=True)


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
        self.pro_mode = 0


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
                label=f"Mode 0 - {len(generate_time_slots(0))} slots ({_SLOT_MODE_HINT[0]})",
                value="0", default=True),
            discord.SelectOption(
                label=f"Mode 1 - {len(generate_time_slots(1))} slots ({_SLOT_MODE_HINT[1]})",
                value="1"),
        ]
        super().__init__(placeholder="Pick the slot grid mode", options=options, min_values=1, max_values=1)
        self.wizard_view = wizard_view

    async def callback(self, interaction: discord.Interaction):
        self.wizard_view.draft.slot_mode = int(self.values[0])
        await self.wizard_view.refresh(interaction)


class _ProModeSelect(discord.ui.Select):
    def __init__(self, wizard_view: "_KvkWizardView"):
        options = [
            discord.SelectOption(label="Standard mode", value="0", default=True),
            discord.SelectOption(
                label="Pro mode", value="1",
                description="Training day collects troop levels and scores KvK points"),
        ]
        super().__init__(placeholder="Pick the event mode", options=options, min_values=1, max_values=1)
        self.wizard_view = wizard_view

    async def callback(self, interaction: discord.Interaction):
        self.wizard_view.draft.pro_mode = int(self.values[0])
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
        self.add_item(_ProModeSelect(self))
        self.add_item(_TypesSelect(self))
        self.add_item(_PublishChannelSelect(self))
        confirm_button = discord.ui.Button(
            label="Confirm and create", style=discord.ButtonStyle.primary, row=4)
        confirm_button.callback = self.confirm
        self.add_item(confirm_button)

    def build_embed(self) -> discord.Embed:
        d = self.draft
        if d.active_types:
            types_text = "\n".join(
                f"{t}: {date}" for t, date in type_dates_for(d.event_date, d.active_types))
        else:
            types_text = "(pick at least one)"
        channel_text = f"<#{d.publish_channel_id}>" if d.publish_channel_id else "(pick a channel)"
        n_text = str(d.slots_per_alliance) if d.scope == "alliance" else "n/a (kingdom scope)"
        embed = discord.Embed(title="Create KvK Event", color=discord.Color.blue())
        embed.add_field(name="Name", value=d.name, inline=False)
        embed.add_field(name="Event date", value=d.event_date, inline=True)
        embed.add_field(name="Scope", value=d.scope, inline=True)
        embed.add_field(name="Slots per alliance", value=n_text, inline=True)
        embed.add_field(
            name="Slot mode", value=f"{d.slot_mode} ({_SLOT_MODE_HINT[d.slot_mode]})", inline=True)
        embed.add_field(name="Mode", value="Pro" if d.pro_mode else "Standard", inline=True)
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

        type_dates = type_dates_for(d.event_date, d.active_types)
        event_id = kvkdb.create_event(
            self.cog.conn,
            guild_id=d.guild_id,
            name=d.name,
            event_date=d.event_date,
            scope=d.scope,
            slots_per_alliance=d.slots_per_alliance,
            slot_mode=d.slot_mode,
            signup_open_at=d.signup_open_at,
            signup_close_at=d.signup_close_at,
            publish_channel_id=d.publish_channel_id,
            created_by=d.created_by,
            created_at=datetime.now(UTC).strftime(DT_FMT),
            pro_mode=d.pro_mode,
        )
        kvkdb.set_event_types(self.cog.conn, event_id, type_dates)

        posted = await _post_announcement(interaction.client, d, event_id, type_dates)

        result_embed = discord.Embed(title="KvK event created", color=discord.Color.green())
        result_embed.add_field(name="Event ID", value=str(event_id), inline=True)
        result_embed.add_field(name="Name", value=d.name, inline=True)
        result_embed.add_field(
            name="Type dates",
            value="\n".join(f"{t}: {date}" for t, date in type_dates),
            inline=False,
        )
        if not posted:
            result_embed.add_field(
                name="Announcement",
                value="The bot could not post to that channel. Check its permissions there.",
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
        description="Run `/kvk_signup` to sign up.",
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


class _FidSelect(discord.ui.Select):
    def __init__(self, cog: KvkScheduling, event_id: int, fids: list[int]):
        options = [discord.SelectOption(label=str(fid), value=str(fid)) for fid in fids]
        super().__init__(placeholder="Pick your fid", options=options, min_values=1, max_values=1)
        self.cog = cog
        self.event_id = event_id

    async def callback(self, interaction: discord.Interaction):
        await self.cog._start_signup(interaction, self.event_id, int(self.values[0]), edit=True)


class _FidSelectView(discord.ui.View):
    """Lets a player with more than one linked fid pick which one to sign up with."""

    def __init__(self, cog: KvkScheduling, event_id: int, fids: list[int]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.add_item(_FidSelect(cog, event_id, fids))


class _EventSignupSelect(discord.ui.Select):
    def __init__(self, cog: KvkScheduling, events: list, fids: list[int]):
        options = [
            discord.SelectOption(
                label=e["name"][:100], value=str(e["id"]), description=e["event_date"],
                emoji=_status_icon("collecting"))
            for e in events[:_MAX_EVENT_OPTIONS]
        ]
        super().__init__(placeholder="Pick an event", options=options, min_values=1, max_values=1)
        self.cog = cog
        self.fids = fids

    async def callback(self, interaction: discord.Interaction):
        await self.cog._pick_fid_then_signup(interaction, int(self.values[0]), self.fids, edit=True)


class _EventSignupSelectView(discord.ui.View):
    """Lets a player pick which open KvK event to sign up for when more than one is open."""

    def __init__(self, cog: KvkScheduling, events: list, fids: list[int]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.add_item(_EventSignupSelect(cog, events, fids))


class _SignupTypeSelect(discord.ui.Select):
    def __init__(self, types_view: "_SignupTypesView", active_types: list[str]):
        options = [discord.SelectOption(label=t, value=t) for t in active_types]
        super().__init__(
            placeholder="Pick position types (up to 3)", options=options,
            min_values=1, max_values=min(3, len(active_types)))
        self.types_view = types_view

    async def callback(self, interaction: discord.Interaction):
        self.types_view.selected_types = list(self.values)
        await interaction.response.edit_message(
            content=f"Picked: {', '.join(self.types_view.selected_types)}. "
                    f"Press Enter speedups to submit your times.",
            view=self.types_view)


class _SignupTypesView(discord.ui.View):
    """Step 1 of signup: pick position types, then press Enter speedups to open the modal."""

    def __init__(self, cog: KvkScheduling, event_id: int, fid: int, active_types: list[str]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.event_id = event_id
        self.fid = fid
        self.selected_types: list[str] = []
        self.add_item(_SignupTypeSelect(self, active_types))
        enter_button = discord.ui.Button(label="Enter speedups", style=discord.ButtonStyle.primary)
        enter_button.callback = self.enter_speedups
        self.add_item(enter_button)

    async def enter_speedups(self, interaction: discord.Interaction):
        if not self.selected_types:
            await interaction.response.send_message("Pick at least one position type first.", ephemeral=True)
            return
        ev = kvkdb.get_event(self.cog.conn, self.event_id)
        pro_training = bool(ev and ev["pro_mode"]) and "Training" in self.selected_types
        non_training = [t for t in self.selected_types if t != "Training"]
        if pro_training and not non_training:
            await interaction.response.send_modal(_ProTrainingModal(self.cog, self.event_id, self.fid))
        elif pro_training:
            await interaction.response.send_modal(
                _SignupSpeedupModal(self.cog, self.event_id, self.fid, non_training, then_pro_training=True))
        else:
            await interaction.response.send_modal(
                _SignupSpeedupModal(self.cog, self.event_id, self.fid, self.selected_types))


class _SignupSpeedupModal(discord.ui.Modal, title="KvK Signup Speedups"):
    """Step 2 of signup: one speedup input per chosen position type."""

    def __init__(self, cog: KvkScheduling, event_id: int, fid: int, position_types: list[str],
                 then_pro_training: bool = False):
        super().__init__()
        self.cog = cog
        self.event_id = event_id
        self.fid = fid
        self.then_pro_training = then_pro_training
        self.speedup_inputs: dict[str, discord.ui.TextInput] = {}
        for position_type in position_types:
            text_input = discord.ui.TextInput(label=f"{position_type} speedup (e.g. 7d 12h)", max_length=50)
            self.speedup_inputs[position_type] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        parsed: dict[str, int] = {}
        for position_type, text_input in self.speedup_inputs.items():
            try:
                parsed[position_type] = parse_speedups(text_input.value)
            except ValueError:
                await interaction.response.send_message(
                    f"Could not read the {position_type} speedup value. Use a format like '7d 12h'.",
                    ephemeral=True)
                return

        submitted_at = datetime.now(UTC).strftime(DT_FMT)
        for position_type, minutes in parsed.items():
            kvkdb.upsert_signup(
                self.cog.conn, self.event_id, self.fid, position_type, minutes,
                interaction.user.id, submitted_at)

        ev = kvkdb.get_event(self.cog.conn, self.event_id)
        event_name = ev["name"] if ev else str(self.event_id)
        embed = discord.Embed(
            title=f"{theme.verifiedIcon} Signup saved",
            description=f"KvK: **{event_name}**\nPlayer fid: `{self.fid}`",
            color=discord.Color.green())
        for position_type, minutes in parsed.items():
            embed.add_field(name=position_type, value=format_speedups(minutes), inline=True)
        # Preferred times only make sense for kingdom scope (full-day grid); alliance slots are
        # the first N times of the grid, so a preference there could not be honored.
        view = None
        if self.then_pro_training:
            embed.set_footer(text="Now enter your Training details for Pro scoring.")
            view = _ProTrainingEntryView(self.cog, self.event_id, self.fid)
        elif ev and ev["scope"] == "kingdom":
            embed.set_footer(text="Optional: add preferred times to say which slots you want.")
            view = _PreferredTimesEntryView(self.cog, self.event_id, self.fid, list(parsed.keys()))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class _ProTrainingEntryView(discord.ui.View):
    """After non-training speedups are saved in a Pro event, opens the Pro training-day modal."""

    def __init__(self, cog: KvkScheduling, event_id: int, fid: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.event_id = event_id
        self.fid = fid
        button = discord.ui.Button(label="Enter Training details", style=discord.ButtonStyle.primary)
        button.callback = self.enter
        self.add_item(button)

    async def enter(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ProTrainingModal(self.cog, self.event_id, self.fid))


class _ProTrainingModal(discord.ui.Modal, title="KvK Pro Training"):
    """Pro-mode Training day: base level + hours (+ optional troop upgrade) -> computed KvK points."""

    def __init__(self, cog: KvkScheduling, event_id: int, fid: int):
        super().__init__()
        self.cog = cog
        self.event_id = event_id
        self.fid = fid
        self.base = discord.ui.Label(
            text="Base troop level",
            component=discord.ui.Select(
                options=[discord.SelectOption(label=x, value=x) for x in TROOP_LEVELS],
                min_values=1, max_values=1))
        self.add_item(self.base)
        self.hours = discord.ui.Label(
            text="Hours of speedups", description="e.g. 20h or 1d 8h",
            component=discord.ui.TextInput(placeholder="20h", max_length=50))
        self.add_item(self.hours)
        self.upgrade = discord.ui.Label(
            text="Upgrade from level (optional)",
            component=discord.ui.Select(
                options=[discord.SelectOption(label=f"T{n}", value=str(n)) for n in range(1, 11)],
                min_values=0, max_values=1))
        self.add_item(self.upgrade)
        self.count = discord.ui.Label(
            text="Upgrade count (optional)", description="troops to upgrade, e.g. 900k",
            component=discord.ui.TextInput(required=False, placeholder="900k", max_length=20))
        self.add_item(self.count)

    async def on_submit(self, interaction: discord.Interaction):
        base_vals = self.base.component.values
        if not base_vals:
            await interaction.response.send_message("Pick a base troop level.", ephemeral=True)
            return
        base_label = base_vals[0]
        try:
            base_tier = troop_tier(base_label)
            hours_minutes = parse_speedups(self.hours.component.value)
        except ValueError:
            await interaction.response.send_message(
                "Could not read the hours. Use a format like '20h' or '1d 8h'.", ephemeral=True)
            return
        upgrade_vals = self.upgrade.component.values
        upgrade_from = int(upgrade_vals[0]) if upgrade_vals else None
        try:
            upgrade_count = parse_troop_count(self.count.component.value)
            result = compute_training_points(base_tier, hours_minutes, upgrade_from, upgrade_count)
        except ValueError as exc:
            await interaction.response.send_message(f"{exc}. Fix and try again.", ephemeral=True)
            return

        submitted_at = datetime.now(UTC).strftime(DT_FMT)
        kvkdb.upsert_signup(
            self.cog.conn, self.event_id, self.fid, "Training", hours_minutes, interaction.user.id, submitted_at)
        kvkdb.set_pro_training(
            self.cog.conn, self.event_id, self.fid, base_label, upgrade_from, upgrade_count, result["kvk_points"])

        ev = kvkdb.get_event(self.cog.conn, self.event_id)
        embed = discord.Embed(
            title=f"{theme.verifiedIcon} Training signup saved",
            description=f"KvK: **{ev['name'] if ev else self.event_id}**\nPlayer fid: `{self.fid}`",
            color=discord.Color.green())
        embed.add_field(name="Base level", value=base_label, inline=True)
        embed.add_field(name="Hours", value=format_speedups(hours_minutes), inline=True)
        if upgrade_from is not None and result["upgraded"]:
            embed.add_field(
                name="Upgrades",
                value=f"{result['upgraded']:,} from T{upgrade_from} = {result['upgrade_points']:,} pts",
                inline=False)
        embed.add_field(
            name="New troops",
            value=f"{result['new_troops']:,} x {base_label} = {result['new_points']:,} pts", inline=False)
        embed.add_field(name="KvK points", value=f"**{result['kvk_points']:,}**", inline=False)
        view = None
        if ev and ev["scope"] == "kingdom":
            embed.set_footer(text="Optional: add preferred times to say which slots you want.")
            view = _PreferredTimesEntryView(self.cog, self.event_id, self.fid, ["Training"])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class _PreferredTimesEntryView(discord.ui.View):
    """After a signup is saved, offers an optional step to add preferred times."""

    def __init__(self, cog: KvkScheduling, event_id: int, fid: int, position_types: list[str]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.event_id = event_id
        self.fid = fid
        self.position_types = position_types
        button = discord.ui.Button(
            label="Add preferred times", emoji=theme.timeIcon, style=discord.ButtonStyle.primary)
        button.callback = self.add_times
        self.add_item(button)

    async def add_times(self, interaction: discord.Interaction):
        ev = kvkdb.get_event(self.cog.conn, self.event_id)
        if ev is None:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _PreferredTimesModal(self.cog, self.event_id, self.fid, self.position_types, ev["slot_mode"]))


class _PreferredTimesModal(discord.ui.Modal, title="KvK Preferred Times"):
    """Optional: one preferred-times field per type (free text, e.g. '20:00-22:00, 23:30'). Auto-assign
    tries to give higher-speedup players their preferred slots first."""

    def __init__(self, cog: KvkScheduling, event_id: int, fid: int, position_types: list[str], slot_mode: int):
        super().__init__()
        self.cog = cog
        self.event_id = event_id
        self.fid = fid
        self.slot_mode = slot_mode
        self.time_inputs: dict[str, discord.ui.TextInput] = {}
        for position_type in position_types:
            text_input = discord.ui.TextInput(
                label=f"{position_type} times", required=False, max_length=100,
                placeholder="e.g. 20:00-22:00, 23:30 (blank = any slot)")
            self.time_inputs[position_type] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        parsed: dict[str, list[int]] = {}
        for position_type, text_input in self.time_inputs.items():
            try:
                parsed[position_type] = parse_desired_slots(text_input.value, self.slot_mode)
            except ValueError:
                await interaction.response.send_message(
                    f"Could not read the {position_type} times. Use a format like '20:00-22:00, 23:30'.",
                    ephemeral=True)
                return
        for position_type, indices in parsed.items():
            kvkdb.set_desired_slots(
                self.cog.conn, self.event_id, self.fid, position_type, ",".join(str(i) for i in indices))

        times = generate_time_slots(self.slot_mode)
        embed = discord.Embed(title=f"{theme.verifiedIcon} Preferred times saved", color=discord.Color.green())
        for position_type, indices in parsed.items():
            picks = [times[i] for i in indices if i < len(times)]
            embed.add_field(name=position_type, value=", ".join(picks) if picks else "(any slot)", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class _KvkEventSelect(discord.ui.Select):
    def __init__(self, menu_view: "_KvkMenuView", events: list):
        options = [
            discord.SelectOption(
                label=e["name"][:100],
                value=str(e["id"]),
                description=f"{e['event_date']} - {e['scope']} - {e['status']}"[:100],
                emoji=_status_icon(e["status"]),
                default=(e["id"] == menu_view.selected_event_id),
            )
            for e in events
        ]
        super().__init__(placeholder="Pick an event", options=options, min_values=1, max_values=1, row=0)
        self.menu_view = menu_view

    async def callback(self, interaction: discord.Interaction):
        self.menu_view.selected_event_id = int(self.values[0])
        self.menu_view._build_items()  # rebuild so the dropdown shows the pick and any new events
        await safe_edit_message(interaction, embed=self.menu_view.build_embed(), view=self.menu_view)


class _EditSignupModal(discord.ui.Modal, title="Edit a KvK Signup"):
    """Asks for a player's fid, then opens the admin signup editor for that fid."""

    def __init__(self, cog: KvkScheduling, event_id: int):
        super().__init__()
        self.cog = cog
        self.event_id = event_id
        self.fid_input = discord.ui.TextInput(label="Player fid", max_length=20)
        self.add_item(self.fid_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            fid = int(self.fid_input.value.strip())
        except ValueError:
            await interaction.response.send_message("The fid must be a whole number.", ephemeral=True)
            return
        await self.cog._start_signup(interaction, self.event_id, fid, admin_override=True)


class _KvkMenuView(discord.ui.View):
    """The /settings KvK sub-menu: pick an event, then create, report, publish, or edit signups."""

    def __init__(self, cog: KvkScheduling, guild_id: int, viewer_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.guild_id = guild_id
        self.viewer_id = viewer_id
        self.selected_event_id: int | None = None
        self.events: list = []
        self._build_items()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Lock the menu to the admin who opened it. The /settings message is public, so without
        this any member could click the buttons; the edit path in particular has no other admin gate."""
        if not await check_interaction_user(interaction, self.viewer_id):
            return False
        if not self.cog._is_global_admin(interaction):
            await interaction.response.send_message(
                f"{theme.deniedIcon} KvK Scheduling is Global Admin only.", ephemeral=True)
            return False
        return True

    def _build_items(self) -> None:
        """(Re)build the components from the current event set. The event select and the actions
        that need a selected event only appear when at least one event exists (Discord needs 1-25
        select options)."""
        self.clear_items()
        self.events = kvkdb.list_events(self.cog.conn, self.guild_id)
        if self.selected_event_id not in {e["id"] for e in self.events}:
            self.selected_event_id = None

        if self.events:
            self.add_item(_KvkEventSelect(self, self.events[:_MAX_EVENT_OPTIONS]))

        create_button = discord.ui.Button(
            label="Create Event", emoji=theme.addIcon, style=discord.ButtonStyle.success, row=1)
        create_button.callback = self.create
        self.add_item(create_button)

        if self.events:
            report_button = discord.ui.Button(label="Report / Assign", style=discord.ButtonStyle.primary, row=1)
            report_button.callback = self.report
            self.add_item(report_button)

            publish_button = discord.ui.Button(label="Publish", style=discord.ButtonStyle.secondary, row=1)
            publish_button.callback = self.publish
            self.add_item(publish_button)

            edit_button = discord.ui.Button(label="Edit a Signup", style=discord.ButtonStyle.secondary, row=1)
            edit_button.callback = self.edit_signup
            self.add_item(edit_button)

            delete_button = discord.ui.Button(
                label="Delete", emoji=theme.trashIcon, style=discord.ButtonStyle.danger, row=1)
            delete_button.callback = self.delete
            self.add_item(delete_button)

            view_button = discord.ui.Button(
                label="View signups", emoji=theme.eyeIcon, style=discord.ButtonStyle.secondary, row=2)
            view_button.callback = self.view
            self.add_item(view_button)

        back_button = discord.ui.Button(
            label="Back", emoji=theme.backIcon, style=discord.ButtonStyle.secondary, row=2)
        back_button.callback = self.back
        self.add_item(back_button)

    @property
    def truncated(self) -> bool:
        return len(self.events) > _MAX_EVENT_OPTIONS

    def build_embed(self) -> discord.Embed:
        actions = "\n".join([
            f"{theme.addIcon} **Create Event** - start the setup wizard",
            f"{theme.chartIcon} **Report / Assign** - rank signups and place slots",
            f"{theme.announceIcon} **Publish** - post the schedule to its channel",
            f"{theme.editListIcon} **Edit a Signup** - fix one player's speedups",
            f"{theme.trashIcon} **Delete** - remove the event and its data",
        ])
        embed = discord.Embed(
            title=f"{theme.crossIcon} KvK Scheduling",
            description=f"Players sign up with `/kvk_signup`. Admin actions:\n{actions}",
            color=discord.Color.blue())
        if self.selected_event_id is not None:
            self._add_event_fields(embed)
        elif self.events:
            embed.add_field(
                name="No event picked", value="Pick one from the dropdown, then use the buttons.", inline=False)
        else:
            embed.add_field(name="No events yet", value="Use Create Event to make one.", inline=False)
        if self.truncated:
            embed.set_footer(text=f"Showing the newest {_MAX_EVENT_OPTIONS} of {len(self.events)} events.")
        return embed

    def _registration_status(self, ev: dict) -> str:
        """Is signup open right now for this event? Mirrors list_open_events (status + window)."""
        if ev["status"] != "collecting":
            return f"{theme.deniedIcon} closed (status: {ev['status']})"
        now = datetime.now(UTC).strftime(DT_FMT)
        if now < ev["signup_open_at"]:
            return f"{theme.hourglassIcon} not open yet (opens {ev['signup_open_at']} UTC)"
        if now > ev["signup_close_at"]:
            return f"{theme.deniedIcon} closed (ended {ev['signup_close_at']} UTC)"
        return f"{theme.verifiedIcon} OPEN now (until {ev['signup_close_at']} UTC)"

    def _add_event_fields(self, embed: discord.Embed) -> None:
        """Structured details of the selected event, as embed fields for readability."""
        ev = kvkdb.get_event(self.cog.conn, self.selected_event_id)
        if ev is None:
            embed.add_field(
                name="Event gone", value=f"Event {self.selected_event_id} no longer exists. Pick another.",
                inline=False)
            return
        counts: dict = {}
        for _fid, ptype in kvkdb.get_signup_minutes(self.cog.conn, self.selected_event_id):
            counts[ptype] = counts.get(ptype, 0) + 1
        type_dates = kvkdb.get_event_type_dates(self.cog.conn, self.selected_event_id)
        type_lines = "\n".join(
            f"- {t}: {d} ({counts.get(t, 0)} signups)" for t, d in type_dates) or "(none)"
        n_text = f"{ev['slots_per_alliance']} slots per alliance" if ev["scope"] == "alliance" else "kingdom-wide"
        channel = f"<#{ev['publish_channel_id']}>" if ev["publish_channel_id"] else "(none)"
        icon = _status_icon(ev["status"]) or ""

        embed.add_field(name=f"{icon} {ev['name']}", value=f"id {ev['id']} - status **{ev['status']}**", inline=False)
        embed.add_field(name=f"{theme.calendarIcon} Event date", value=ev["event_date"], inline=True)
        embed.add_field(name=f"{theme.globeIcon} Scope", value=f"{ev['scope']} ({n_text})", inline=True)
        embed.add_field(
            name="Slot mode", value=f"{ev['slot_mode']} - {_SLOT_MODE_HINT.get(ev['slot_mode'], '')}", inline=True)
        embed.add_field(
            name="Registration",
            value=f"{ev['signup_open_at']} to {ev['signup_close_at']} UTC\n{self._registration_status(ev)}",
            inline=False)
        embed.add_field(name="Publish channel", value=channel, inline=True)
        embed.add_field(name=f"{theme.membersIcon} Positions", value=type_lines, inline=False)

    async def _require_event(self, interaction: discord.Interaction) -> bool:
        if self.selected_event_id is None:
            await interaction.response.send_message("Pick an event first.", ephemeral=True)
            return False
        return True

    def _report_cog(self):
        return self.cog.bot.get_cog("KvkReport")

    async def back(self, interaction: discord.Interaction):
        main_menu_cog = self.cog.bot.get_cog("MainMenu")
        if main_menu_cog:
            await main_menu_cog.show_main_menu(interaction)
        else:
            await interaction.response.send_message(
                f"{theme.deniedIcon} Main Menu module not found.", ephemeral=True)

    async def create(self, interaction: discord.Interaction):
        await self.cog.launch_create(interaction)

    async def report(self, interaction: discord.Interaction):
        if not await self._require_event(interaction):
            return
        report_cog = self._report_cog()
        if report_cog is None:
            await interaction.response.send_message(
                f"{theme.deniedIcon} KvK Report module not found.", ephemeral=True)
            return
        await report_cog.launch_report(interaction, self.selected_event_id)

    async def publish(self, interaction: discord.Interaction):
        if not await self._require_event(interaction):
            return
        report_cog = self._report_cog()
        if report_cog is None:
            await interaction.response.send_message(
                f"{theme.deniedIcon} KvK Report module not found.", ephemeral=True)
            return
        await report_cog.launch_publish(interaction, self.selected_event_id)

    async def edit_signup(self, interaction: discord.Interaction):
        if not await self._require_event(interaction):
            return
        await interaction.response.send_modal(_EditSignupModal(self.cog, self.selected_event_id))

    async def view(self, interaction: discord.Interaction):
        if not await self._require_event(interaction):
            return
        report_cog = self._report_cog()
        if report_cog is None:
            await interaction.response.send_message(
                f"{theme.deniedIcon} KvK Report module not found.", ephemeral=True)
            return
        await report_cog.launch_view_signups(interaction, self.selected_event_id)

    async def delete(self, interaction: discord.Interaction):
        if not await self._require_event(interaction):
            return
        ev = kvkdb.get_event(self.cog.conn, self.selected_event_id)
        name = ev["name"] if ev else str(self.selected_event_id)
        await interaction.response.edit_message(
            content=f"Delete '{name}' (id {self.selected_event_id})? This removes its signups and slots "
                    f"and cannot be undone.",
            embed=None, view=_ConfirmDeleteView(self, self.selected_event_id))


class _ConfirmDeleteView(discord.ui.View):
    """Yes/Cancel gate before deleting an event; both paths return to the refreshed KvK menu."""

    def __init__(self, menu_view: _KvkMenuView, event_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.menu_view = menu_view
        self.event_id = event_id
        yes_button = discord.ui.Button(label="Yes, delete", style=discord.ButtonStyle.danger)
        yes_button.callback = self.confirm
        self.add_item(yes_button)
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.menu_view.interaction_check(interaction)  # same opener + admin lock

    async def confirm(self, interaction: discord.Interaction):
        kvkdb.delete_event(self.menu_view.cog.conn, self.event_id)
        if self.menu_view.selected_event_id == self.event_id:
            self.menu_view.selected_event_id = None
        self.menu_view._build_items()
        await interaction.response.edit_message(
            content=None, embed=self.menu_view.build_embed(), view=self.menu_view)

    async def cancel(self, interaction: discord.Interaction):
        self.menu_view._build_items()
        await interaction.response.edit_message(
            content=None, embed=self.menu_view.build_embed(), view=self.menu_view)


async def setup(bot):
    await bot.add_cog(KvkScheduling(bot))

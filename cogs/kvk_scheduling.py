"""KvK scheduler cog: event lifecycle and self-service signup."""
import contextlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from . import kvk_alliances
from . import kvk_data as kvkdb
from .kvk_util import (
    POSITION_TYPES,
    SHARED_TRAINING_BONUS,
    TROOP_LEVELS,
    compute_training_points,
    format_speedups,
    generate_time_slots,
    next_kvk_date,
    parse_desired_slots,
    parse_percent,
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


_POSITION_ICON = {
    "Training": theme.trainingIcon, "Research": theme.researchIcon, "Building": theme.constructionIcon}


def _rich_position_lines(type_dates) -> str:
    """'<icon> **Type** - date' lines for the event cards, in the given (day) order."""
    return "\n".join(f"{_POSITION_ICON.get(t, '')} **{t}** - {date}" for t, date in type_dates)


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

            aid = kvk_alliances.alliance_id_of(fid)
            if aid is None:
                await self._send_or_edit(
                    interaction,
                    f"{theme.deniedIcon} You must be bound to an alliance to sign up. "
                    f"Set your alliance first, then try again.", edit=edit)
                return
            if not ev["free_mode"]:
                added = {a["alliance_id"] for a in kvkdb.get_event_alliances(self.conn, event_id)}
                if aid not in added:
                    await self._send_or_edit(
                        interaction,
                        f"{theme.deniedIcon} Your alliance is not part of this KvK event. "
                        f"Ask an admin to add it, or use a different event.", edit=edit)
                    return

        active_types = kvkdb.get_active_types(self.conn, event_id)
        if not active_types:
            await self._send_or_edit(interaction, f"Event {event_id} has no active position types.", edit=edit)
            return

        if ev["pro_mode"]:
            # Pro events: a hub with one button per position, each opening that position's own complete
            # modal (Training's needs 5 fields on its own), so nothing is left half-filled.
            view = _SignupHubView(self, event_id, fid, active_types)
            await self._send_or_edit(interaction, view.content(), view=view, edit=edit)
            with contextlib.suppress(discord.HTTPException):
                view.message = await interaction.original_response()  # so the modals can refresh it
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

    def __init__(self, guild_id: int, created_by: int, name: str, event_date: str,
                 signup_open_at: str, signup_close_at: str):
        self.guild_id = guild_id
        self.created_by = created_by
        self.name = name
        self.event_date = event_date
        self.signup_open_at = signup_open_at
        self.signup_close_at = signup_close_at
        self.slot_mode = 0
        self.active_types: list[str] = []
        self.publish_channel_id: int | None = None
        self.pro_mode = 0


class _KvkCreateModal(discord.ui.Modal, title="Create KvK Event"):
    """First step: name, event date, and the signup window. Alliances and free mode are set after."""

    def __init__(self, cog: KvkScheduling):
        super().__init__()
        self.cog = cog
        # Pre-fill the next KvK: start = next KvK Monday, signups run the weekend before (Fri 00:00 to
        # the Monday 00:00, i.e. through Sunday). All editable.
        start = next_kvk_date(datetime.now(UTC).date())
        start_str = start.strftime(DATE_FMT)
        open_str = (start - timedelta(days=3)).strftime(DATE_FMT) + " 00:00"
        close_str = start_str + " 00:00"

        self.name_input = discord.ui.TextInput(
            label="Event name", max_length=100, default=f"KvK {start_str}")
        self.add_item(self.name_input)
        self.event_date_input = discord.ui.TextInput(
            label="Event date (YYYY-MM-DD)", placeholder="2026-09-01", max_length=10, default=start_str)
        self.add_item(self.event_date_input)
        self.signup_open_input = discord.ui.TextInput(
            label="Signup opens (YYYY-MM-DD HH:MM UTC)", placeholder="2026-08-20 00:00", max_length=16,
            default=open_str)
        self.add_item(self.signup_open_input)
        self.signup_close_input = discord.ui.TextInput(
            label="Signup closes (YYYY-MM-DD HH:MM UTC)", placeholder="2026-08-31 00:00", max_length=16,
            default=close_str)
        self.add_item(self.signup_close_input)

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

        draft = _KvkDraft(
            guild_id=interaction.guild_id,
            created_by=interaction.user.id,
            name=name,
            event_date=event_date,
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
        for opt in self.options:  # keep the choice shown after the view refreshes
            opt.default = opt.value == self.values[0]
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
        for opt in self.options:  # keep the choice shown after the view refreshes
            opt.default = opt.value == self.values[0]
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
        for opt in self.options:  # keep the picks shown after the view refreshes
            opt.default = opt.value in self.values
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
        self.default_values = [  # keep the channel shown after the view refreshes
            discord.SelectDefaultValue(id=self.values[0].id, type=discord.SelectDefaultValueType.channel)]
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
        embed = discord.Embed(title="Create KvK Event", color=discord.Color.blue())
        embed.add_field(name="Name", value=d.name, inline=False)
        embed.add_field(name="Event date", value=d.event_date, inline=True)
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

        type_dates = type_dates_for(d.event_date, d.active_types)
        # New events start in free mode so the announcement that posts now is valid for everyone;
        # the admin can switch to alliance-based and add alliances afterwards.
        event_id = kvkdb.create_event(
            self.cog.conn,
            guild_id=d.guild_id,
            name=d.name,
            event_date=d.event_date,
            free_mode=1,
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

        mode_text = "Pro (troop levels + KvK points)" if d.pro_mode else "Standard"
        description = (
            f"**{d.name}**  (id `{event_id}`)\n"
            f"{theme.upperDivider}\n"
            f"{theme.calendarIcon} **Event date** - {d.event_date}\n"
            f"{theme.timeIcon} **Slot mode** - {d.slot_mode} ({_SLOT_MODE_HINT[d.slot_mode]})\n"
            f"{theme.boltIcon} **Mode** - {mode_text}\n"
            f"{theme.alarmClockIcon} **Signup (UTC)** - {d.signup_open_at} to {d.signup_close_at}\n"
            f"{theme.announceIcon} **Publish** - <#{d.publish_channel_id}>\n"
            f"{theme.middleDivider}\n"
            f"{theme.membersIcon} **Position days**\n{_rich_position_lines(type_dates)}"
        )
        result_embed = discord.Embed(
            title=f"{theme.verifiedIcon} KvK event created",
            description=description, color=discord.Color.green())
        if not posted:
            result_embed.add_field(
                name="Announcement",
                value="The bot could not post to that channel. Check its permissions there.", inline=False)
        result_embed.set_footer(
            text="Open registration by default. To limit to specific alliances: pick this event in the "
                 "KvK menu, Switch to alliances, then Add alliance.")
        await interaction.response.edit_message(embed=result_embed, view=None)


class _SignupButton(discord.ui.DynamicItem[discord.ui.Button], template=r"kvksignup:(?P<event_id>\d+)"):
    """Persistent 'Sign up' button on the public announcement; the custom_id carries the event id."""

    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(discord.ui.Button(
            label="Sign up", style=discord.ButtonStyle.success, emoji=theme.verifiedIcon,
            custom_id=f"kvksignup:{event_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("KvkScheduling")
        if cog is None:
            await interaction.response.send_message("KvK Scheduling is not available right now.", ephemeral=True)
            return
        fids = cog._fids_for_discord(interaction.user.id)
        if not fids:
            await interaction.response.send_message("No linked fid found. Run /register first.", ephemeral=True)
            return
        await cog._pick_fid_then_signup(interaction, self.event_id, fids)


async def _post_announcement(
    client: discord.Client, draft: _KvkDraft, event_id: int, type_dates: list[tuple[str, str]]
) -> bool:
    """Post the event announcement (with a Sign up button) to the publish channel. False on failure."""
    channel = client.get_channel(draft.publish_channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(draft.publish_channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    mode_line = f"{theme.boltIcon} **Mode** - Pro (troop levels + KvK points)\n" if draft.pro_mode else ""
    description = (
        f"{theme.crownIcon} A new Kingdom-vs-Kingdom event has opened.\n"
        f"{theme.upperDivider}\n"
        f"{theme.calendarIcon} **Event date** - {draft.event_date}\n"
        f"{theme.alarmClockIcon} **Signup (UTC)** - {draft.signup_open_at} to {draft.signup_close_at}\n"
        f"{mode_line}"
        f"{theme.middleDivider}\n"
        f"{theme.membersIcon} **Position days**\n{_rich_position_lines(type_dates)}\n"
        f"{theme.lowerDivider}\n"
        f"{theme.nextIcon} Press **Sign up** below, or run `/kvk_signup` to join."
    )
    embed = discord.Embed(
        title=f"{theme.crossIcon} KvK: {draft.name} {theme.crossIcon}",
        description=description, color=discord.Color.gold())
    embed.set_footer(text=f"Event #{event_id}")

    view = discord.ui.View(timeout=None)
    view.add_item(_SignupButton(event_id))
    try:
        await channel.send(embed=embed, view=view)
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


class _SignupHubView(discord.ui.View):
    """Pro-event signup hub: one button per position, each opening that position's own complete modal
    (Training -> the 5-field Pro modal, others -> a speedup modal). Shown only for pro events; standard
    events use the type picker. Each button flips to a checkmark once filled, so nothing is half-done."""

    def __init__(self, cog: KvkScheduling, event_id: int, fid: int, active_types: list[str]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.event_id = event_id
        self.fid = fid
        self.active_types = active_types
        self.done: set[str] = set()
        self.message: discord.Message | None = None
        self._build()

    def content(self) -> str:
        done = ", ".join(t for t in self.active_types if t in self.done) or "none yet"
        left = ", ".join(t for t in self.active_types if t not in self.done) or "none"
        return (f"{theme.crossIcon} **Pro signup** - fill each position (every button opens its own form).\n"
                f"{theme.verifiedIcon} Done: **{done}**   |   Left: **{left}**")

    def _build(self) -> None:
        self.clear_items()
        for position_type in self.active_types:
            is_done = position_type in self.done
            button = discord.ui.Button(
                label=f"{position_type} {theme.verifiedIcon}" if is_done else position_type,
                emoji=_POSITION_ICON.get(position_type),
                style=discord.ButtonStyle.success if is_done else discord.ButtonStyle.primary)
            button.callback = self._position_cb(position_type)
            self.add_item(button)
        pref = discord.ui.Button(
            label="Preferred times", emoji=theme.timeIcon, style=discord.ButtonStyle.secondary)
        pref.callback = self._preferred
        self.add_item(pref)

    def _position_cb(self, position_type: str):
        async def callback(interaction: discord.Interaction):
            if position_type == "Training":
                modal = _ProTrainingModal(self.cog, self.event_id, self.fid, hub=self)
            else:
                modal = _SignupSpeedupModal(self.cog, self.event_id, self.fid, [position_type], hub=self)
            await interaction.response.send_modal(modal)
        return callback

    async def mark_done(self, position_type: str) -> None:
        """Flip a position to done and refresh the hub message in place."""
        self.done.add(position_type)
        self._build()
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(content=self.content(), view=self)

    async def _preferred(self, interaction: discord.Interaction):
        done = [t for t in self.active_types if t in self.done]
        if not done:
            await interaction.response.send_message(
                "Fill at least one position first, then add preferred times.", ephemeral=True)
            return
        ev = kvkdb.get_event(self.cog.conn, self.event_id)
        await interaction.response.send_modal(
            _PreferredTimesModal(self.cog, self.event_id, self.fid, done, ev["slot_mode"] if ev else 0))


class _SignupTypeSelect(discord.ui.Select):
    def __init__(self, types_view: "_SignupTypesView", active_types: list[str]):
        options = [discord.SelectOption(label=t, value=t) for t in active_types]
        super().__init__(
            placeholder="Pick position types (up to 3)", options=options,
            min_values=1, max_values=min(3, len(active_types)))
        self.types_view = types_view

    async def callback(self, interaction: discord.Interaction):
        self.types_view.selected_types = list(self.values)
        for opt in self.options:  # keep the picks shown after the view refreshes
            opt.default = opt.value in self.values
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
        # Standard events only reach this view; pro events use _SignupHubView instead.
        await interaction.response.send_modal(
            _SignupSpeedupModal(self.cog, self.event_id, self.fid, self.selected_types))


class _SignupSpeedupModal(discord.ui.Modal, title="KvK Signup Speedups"):
    """One speedup input per position type. In a pro event it is opened from the hub for a single
    position (hub set); in a standard event from the type picker for the chosen types (hub None)."""

    def __init__(self, cog: KvkScheduling, event_id: int, fid: int, position_types: list[str], hub=None):
        super().__init__()
        self.cog = cog
        self.event_id = event_id
        self.fid = fid
        self.hub = hub
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

        if self.hub is not None:  # pro-event hub: brief confirm, then flip the position's button to done
            saved = ", ".join(f"{p} {format_speedups(m)}" for p, m in parsed.items())
            await interaction.response.send_message(f"{theme.verifiedIcon} Saved: {saved}", ephemeral=True)
            for position_type in parsed:
                await self.hub.mark_done(position_type)
            return

        ev = kvkdb.get_event(self.cog.conn, self.event_id)
        event_name = ev["name"] if ev else str(self.event_id)
        embed = discord.Embed(
            title=f"{theme.verifiedIcon} Signup saved",
            description=f"KvK: **{event_name}**\nPlayer fid: `{self.fid}`",
            color=discord.Color.green())
        for position_type, minutes in parsed.items():
            embed.add_field(name=position_type, value=format_speedups(minutes), inline=True)
        view = None
        if ev:
            embed.set_footer(text="Optional: add preferred times to say which slots you want.")
            view = _PreferredTimesEntryView(self.cog, self.event_id, self.fid, list(parsed.keys()))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class _ProTrainingModal(discord.ui.Modal, title="KvK Pro Training"):
    """Pro-mode Training day: base level + hours (+ optional troop upgrade) -> computed KvK points."""

    def __init__(self, cog: KvkScheduling, event_id: int, fid: int, hub=None):
        super().__init__()
        self.cog = cog
        self.event_id = event_id
        self.fid = fid
        self.hub = hub
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
        self.speed = discord.ui.Label(
            text="Training speed %", description="Power > Bonus Overview > Military > Training speed",
            component=discord.ui.TextInput(placeholder="123%", max_length=10))
        self.add_item(self.speed)
        self.upgrade = discord.ui.Label(
            text="Upgrade from level (optional)",
            component=discord.ui.Select(
                options=[discord.SelectOption(label=f"T{n}", value=str(n)) for n in range(1, 11)],
                required=False, min_values=0, max_values=1))
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
        try:
            personal_speed = parse_percent(self.speed.component.value)
        except ValueError:
            await interaction.response.send_message(
                "Could not read the training speed. Use a number like '202.9'.", ephemeral=True)
            return
        # Kingdom skill, KvK bonus and the position buff apply to everyone; they stack on the player's
        # own training speed into one total percentage that drives the point math.
        total_speed = personal_speed + SHARED_TRAINING_BONUS
        upgrade_vals = self.upgrade.component.values
        upgrade_from = int(upgrade_vals[0]) if upgrade_vals else None
        try:
            upgrade_count = parse_troop_count(self.count.component.value)
        except ValueError:
            await interaction.response.send_message(
                "Could not read the upgrade count. Use a number like '900k'.", ephemeral=True)
            return
        if (upgrade_from is None) != (upgrade_count == 0):
            await interaction.response.send_message(
                "For an upgrade, set BOTH the upgrade level and the count, or leave both blank.",
                ephemeral=True)
            return
        try:
            result = compute_training_points(
                base_tier, hours_minutes, upgrade_from, upgrade_count, total_speed)
        except ValueError as exc:
            await interaction.response.send_message(f"{exc}. Fix and try again.", ephemeral=True)
            return

        submitted_at = datetime.now(UTC).strftime(DT_FMT)
        kvkdb.upsert_signup(
            self.cog.conn, self.event_id, self.fid, "Training", hours_minutes, interaction.user.id, submitted_at)
        kvkdb.set_pro_training(
            self.cog.conn, self.event_id, self.fid, base_label, upgrade_from, upgrade_count,
            result["kvk_points"], personal_speed)

        ev = kvkdb.get_event(self.cog.conn, self.event_id)
        r = result
        steps = [
            f"`1)` Speedups: **{format_speedups(hours_minutes)}** = {r['total_seconds']:,} s",
            f"`2)` Training speed: {personal_speed:g}% + {SHARED_TRAINING_BONUS:g}% "
            f"(kingdom/KvK/position) = **{total_speed:g}%**",
            f"`3)` Effective time: {r['total_seconds']:,} x {r['speed_mult']:.3f} "
            f"= **{r['effective_seconds']:,} s** (training speed stretches your speedups)",
        ]
        step = 4
        if upgrade_from is not None and r["upgrade_time_each"]:
            steps.append(
                f"`{step})` Upgrade T{upgrade_from}->{base_label}: {r['effective_seconds']:,} / "
                f"{r['upgrade_time_each']} s each = **{r['upgraded']:,}** troops x "
                f"{r['upgrade_point_each']} pts = **{r['upgrade_points']:,}**")
            step += 1
        steps.append(
            f"`{step})` New {base_label}: {r['remaining_seconds']:,} s / {r['new_time_each']} s each "
            f"= **{r['new_troops']:,}** troops x {r['new_point_each']} pts = **{r['new_points']:,}**")
        embed = discord.Embed(
            title=f"{theme.verifiedIcon} Training signup saved",
            description=(
                f"{theme.crownIcon} KvK: **{ev['name'] if ev else self.event_id}**  -  fid `{self.fid}`\n"
                f"{theme.upperDivider}\n"
                f"{theme.targetIcon} These are **potential** points - an estimate. What you really score "
                f"depends on holding the Training slot and the speedups you actually spend.\n"
                f"{theme.middleDivider}\n"
                f"{theme.chartIcon} **How it is counted**\n" + "\n".join(steps)),
            color=discord.Color.green())
        embed.add_field(
            name=f"{theme.medalIcon} Potential KvK points", value=f"**{r['kvk_points']:,}**", inline=False)
        if self.hub is not None:  # pro hub: show the receipt, then flip the Training button to done
            await interaction.response.send_message(embed=embed, ephemeral=True)
            await self.hub.mark_done("Training")
            return
        view = None
        if ev:
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
    """Optional: one preferred-times field per type (free text, e.g. '20:00-22:00, 23:30'). The pick is
    saved and shown in View signups / the report so leaders can place players; for alliance events they
    merge with the other alliances outside the bot."""

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
                description=f"{e['event_date']} - {e['status']}"[:100],
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
        self.message: discord.Message | None = None  # set from the button interaction, for live refresh
        self._build_items()

    async def refresh_card(self) -> None:
        """Rebuild and re-edit the menu message in place. Used by the ephemeral add/remove flows, which
        run on a separate message and so cannot edit this one through their own interaction."""
        if self.message is None:
            return
        self._build_items()
        with contextlib.suppress(discord.HTTPException):
            await self.message.edit(embed=self.build_embed(), view=self)

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

        selected = kvkdb.get_event(self.cog.conn, self.selected_event_id) if self.selected_event_id else None
        if selected is not None:
            toggle = discord.ui.Button(
                label="Switch to alliances" if selected["free_mode"] else "Switch to free reg",
                emoji=theme.globeIcon, style=discord.ButtonStyle.secondary, row=2)
            toggle.callback = self.toggle_free
            self.add_item(toggle)
            if not selected["free_mode"]:
                add_alliance_button = discord.ui.Button(
                    label="Add alliance", emoji=theme.addIcon, style=discord.ButtonStyle.success, row=2)
                add_alliance_button.callback = self.add_alliance
                self.add_item(add_alliance_button)
                remove_alliance_button = discord.ui.Button(
                    label="Remove alliance", style=discord.ButtonStyle.secondary, row=2)
                remove_alliance_button.callback = self.remove_alliance
                self.add_item(remove_alliance_button)

        back_button = discord.ui.Button(
            label="Back", emoji=theme.backIcon, style=discord.ButtonStyle.secondary, row=3)
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

    def _registration_mode(self, ev: dict) -> str:
        """A readable summary of the event's registration mode for the detail card."""
        if ev["free_mode"]:
            return "Free registration - open to any bound player, kingdom-wide grid"
        added = kvkdb.get_event_alliances(self.cog.conn, ev["id"])
        if not added:
            return "Alliance-based - no alliances added yet (use Add alliance)"
        names = dict(kvk_alliances.list_alliances())
        lines = "\n".join(
            f"- {names.get(a['alliance_id'], a['alliance_id'])}: {a['slots']} slots" for a in added)
        text = f"Alliance-based\n{lines}"
        return text if len(text) <= 1000 else text[:990] + "\n... (more)"  # stay under the 1024 field cap

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
        channel = f"<#{ev['publish_channel_id']}>" if ev["publish_channel_id"] else "(none)"
        icon = _status_icon(ev["status"]) or ""

        embed.add_field(name=f"{icon} {ev['name']}", value=f"id {ev['id']} - status **{ev['status']}**", inline=False)
        embed.add_field(name=f"{theme.calendarIcon} Event date", value=ev["event_date"], inline=True)
        embed.add_field(name=f"{theme.globeIcon} Registration", value=self._registration_mode(ev), inline=False)
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

    async def toggle_free(self, interaction: discord.Interaction):
        if not await self._require_event(interaction):
            return
        ev = kvkdb.get_event(self.cog.conn, self.selected_event_id)
        if ev is None:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return
        kvkdb.set_free_mode(self.cog.conn, self.selected_event_id, not ev["free_mode"])
        self._build_items()  # the alliance buttons appear/disappear with the mode
        await safe_edit_message(interaction, embed=self.build_embed(), view=self)

    async def add_alliance(self, interaction: discord.Interaction):
        if not await self._require_event(interaction):
            return
        added = {a["alliance_id"] for a in kvkdb.get_event_alliances(self.cog.conn, self.selected_event_id)}
        available = [(aid, name) for aid, name in kvk_alliances.list_alliances() if aid not in added]
        if not available:
            await interaction.response.send_message("All alliances are already added.", ephemeral=True)
            return
        self.message = interaction.message  # this menu message, so the ephemeral flow can refresh it
        await interaction.response.send_message(
            "Pick an alliance to add, then set its slot count:",
            view=_AddAllianceView(self, self.selected_event_id, available), ephemeral=True)

    async def remove_alliance(self, interaction: discord.Interaction):
        if not await self._require_event(interaction):
            return
        added = kvkdb.get_event_alliances(self.cog.conn, self.selected_event_id)
        if not added:
            await interaction.response.send_message("No alliances to remove.", ephemeral=True)
            return
        self.message = interaction.message
        names = dict(kvk_alliances.list_alliances())
        await interaction.response.send_message(
            "Pick an alliance to remove:",
            view=_RemoveAllianceView(self, self.selected_event_id, added, names), ephemeral=True)


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


class _AddAllianceView(discord.ui.View):
    """Ephemeral: pick one of the not-yet-added alliances, then set its slot count."""

    def __init__(self, menu_view: _KvkMenuView, event_id: int, available: list):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.menu_view = menu_view
        self.add_item(_AddAllianceSelect(menu_view, event_id, available))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.menu_view.interaction_check(interaction)


class _AddAllianceSelect(discord.ui.Select):
    def __init__(self, menu_view: _KvkMenuView, event_id: int, available: list):
        options = [discord.SelectOption(label=name[:100], value=str(aid)) for aid, name in available[:25]]
        super().__init__(placeholder="Pick an alliance", options=options, min_values=1, max_values=1)
        self.menu_view = menu_view
        self.event_id = event_id
        self.names = dict(available)

    async def callback(self, interaction: discord.Interaction):
        aid = int(self.values[0])
        await interaction.response.send_modal(
            _AllianceSlotsModal(self.menu_view, self.event_id, aid, self.names.get(aid, str(aid))))


class _AllianceSlotsModal(discord.ui.Modal, title="Alliance slots"):
    def __init__(self, menu_view: _KvkMenuView, event_id: int, alliance_id: int, alliance_name: str):
        super().__init__()
        self.menu_view = menu_view
        self.event_id = event_id
        self.alliance_id = alliance_id
        self.alliance_name = alliance_name
        self.slots_input = discord.ui.TextInput(
            label=f"Slots for {alliance_name}"[:45], placeholder="10", max_length=4)
        self.add_item(self.slots_input)

    async def on_submit(self, interaction: discord.Interaction):
        conn = self.menu_view.cog.conn
        try:
            slots = int(self.slots_input.value.strip())
        except ValueError:
            await interaction.response.send_message("Slots must be a whole number.", ephemeral=True)
            return
        if slots <= 0:
            await interaction.response.send_message("Slots must be a positive number.", ephemeral=True)
            return
        ev = kvkdb.get_event(conn, self.event_id)
        max_slots = len(generate_time_slots(ev["slot_mode"])) if ev else len(generate_time_slots(0))
        if slots > max_slots:
            await interaction.response.send_message(
                f"Slots ({slots}) is more than the {max_slots}-slot day grid. Pick {max_slots} or fewer.",
                ephemeral=True)
            return
        kvkdb.add_event_alliance(conn, self.event_id, self.alliance_id, slots)
        await interaction.response.send_message(
            f"{theme.verifiedIcon} Added **{self.alliance_name}** with {slots} slots.", ephemeral=True)
        await self.menu_view.refresh_card()  # update the menu card in place


class _RemoveAllianceView(discord.ui.View):
    """Ephemeral: pick one of the event's alliances to remove."""

    def __init__(self, menu_view: _KvkMenuView, event_id: int, added: list, names: dict):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.menu_view = menu_view
        self.add_item(_RemoveAllianceSelect(menu_view, event_id, added, names))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.menu_view.interaction_check(interaction)


class _RemoveAllianceSelect(discord.ui.Select):
    def __init__(self, menu_view: _KvkMenuView, event_id: int, added: list, names: dict):
        options = [
            discord.SelectOption(
                label=f"{names.get(a['alliance_id'], a['alliance_id'])} ({a['slots']} slots)"[:100],
                value=str(a["alliance_id"]))
            for a in added[:25]
        ]
        super().__init__(placeholder="Pick an alliance to remove", options=options, min_values=1, max_values=1)
        self.menu_view = menu_view
        self.event_id = event_id
        self.names = names

    async def callback(self, interaction: discord.Interaction):
        aid = int(self.values[0])
        kvkdb.remove_event_alliance(self.menu_view.cog.conn, self.event_id, aid)
        await interaction.response.send_message(
            f"{theme.verifiedIcon} Removed **{self.names.get(aid, aid)}**.", ephemeral=True)
        await self.menu_view.refresh_card()  # update the menu card in place


async def setup(bot):
    bot.add_dynamic_items(_SignupButton)  # persistent Sign up buttons survive restarts
    await bot.add_cog(KvkScheduling(bot))

"""KvK report: rank submissions, auto-assign slots, admin override, publish."""
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from . import kvk_data as kvkdb
from .kvk_util import generate_time_slots, rank_and_assign

VIEW_TIMEOUT = 7200
_FIELD_LIMIT = 1024
_EMBED_CHAR_BUDGET = 5500  # Discord caps an embed at 6000 chars total; leave headroom for the title
_MAX_FIELDS_PER_EMBED = 25  # Discord's hard field-count cap per embed
_MAX_EMBEDS_PER_MESSAGE = 10
_MAX_GROUP_OPTIONS = 25
_LOCK_MARK = "[LOCKED]"


def _alliance_of(fid: int):
    users = sqlite3.connect("db/users.sqlite")
    try:
        row = users.execute("SELECT alliance FROM users WHERE fid = ?", (fid,)).fetchone()
        return str(row[0]) if row and row[0] is not None else None  # TEXT affinity, keep as str
    finally:
        users.close()


def _alliance_id_of(fid: int):
    """Resolve fid to an int alliance id, or None if unknown/non-numeric (an orphan)."""
    aid = _alliance_of(fid)
    try:
        return int(aid) if aid is not None else None
    except ValueError:
        return None


def _nicknames_for(fids: set) -> dict:
    """Batch-look-up nicknames for a set of fids. One query instead of one per fid."""
    if not fids:
        return {}
    users = sqlite3.connect("db/users.sqlite")
    try:
        placeholders = ",".join("?" * len(fids))
        rows = users.execute(
            f"SELECT fid, nickname FROM users WHERE fid IN ({placeholders})", tuple(fids)).fetchall()
        return dict(rows)
    finally:
        users.close()


def _fmt_minutes(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"


def _group_label(scope: str, position_type: str, alliance_id: int) -> str:
    if scope == "kingdom":
        return f"{position_type} - Kingdom"
    return f"{position_type} - Alliance {alliance_id}"


def _distinct_groups(conn, event_id) -> list:
    """Distinct (position_type, alliance_id) pairs that have slot rows, in query order."""
    seen: dict = {}
    for row in kvkdb.get_slots(conn, event_id):
        key = (row["position_type"], row["alliance_id"])
        seen.setdefault(key, None)
    return list(seen)


def _skipped_fids(conn, event_id, ev: dict) -> list:
    """Signed-up fids with no resolvable alliance, for alliance-scope events.

    Recomputed fresh from current signup data on every call (not cached on the cog), so it is
    always scoped to the one event being rendered and stays correct after Swap/Lock/Unlock/Clear
    refreshes that do not re-run _compute.
    """
    if ev["scope"] != "alliance":
        return []
    skipped = []
    for position_type in kvkdb.get_active_types(conn, event_id):
        for s in kvkdb.get_signups(conn, event_id, position_type):
            if _alliance_id_of(s["fid"]) is None:
                skipped.append(s["fid"])
    return skipped


def _chunk_lines(lines: list, limit: int) -> list:
    """Greedily join lines with newlines, splitting into new chunks before limit is exceeded."""
    if not lines:
        return ["(no slots)"]
    chunks = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _slot_line(row: dict, position_type: str, minutes_map: dict, nicknames: dict) -> str:
    if row["fid"] is None:
        return f"{row['slot_time']} - (empty)"
    nickname = nicknames.get(row["fid"], f"Unknown ({row['fid']})")
    minutes = minutes_map.get((row["fid"], position_type), 0)
    lock = f" {_LOCK_MARK}" if row["locked"] else ""
    return f"{row['slot_time']} - {nickname} ({row['fid']}) - {_fmt_minutes(minutes)}{lock}"


def _add_paged_field(embeds: list, new_embed, used: int, name: str, value: str) -> int:
    """Add a field to embeds[-1], starting a new embed first if the char or field-count cap would overflow.

    Returns the running used-char count for the (possibly new) last embed.
    """
    field_len = len(name) + len(value)
    over_chars = used + field_len > _EMBED_CHAR_BUDGET
    over_fields = len(embeds[-1].fields) >= _MAX_FIELDS_PER_EMBED
    if over_chars or over_fields:
        embeds.append(new_embed)
        used = len(new_embed.title or "")
    embeds[-1].add_field(name=name, value=value, inline=False)
    return used + field_len


def _type_embeds(ev: dict, position_type: str, rows: list, minutes_map: dict, nicknames: dict) -> list:
    """One or more embeds for a single position type, paged under Discord's 6000-char embed cap."""
    groups: list = []
    if ev["scope"] == "kingdom":
        lines = [_slot_line(r, position_type, minutes_map, nicknames) for r in rows]
        groups.append(("Kingdom", lines))
    else:
        by_alliance: dict = {}
        for r in rows:
            by_alliance.setdefault(r["alliance_id"], []).append(r)
        for aid in sorted(by_alliance):
            lines = [_slot_line(r, position_type, minutes_map, nicknames) for r in by_alliance[aid]]
            groups.append((f"Alliance {aid}", lines))

    title = f"{ev['name']} - {position_type}"
    cont_title = f"{title} (cont.)"
    embeds = [discord.Embed(title=title, color=discord.Color.blue())]
    used = len(title)
    for label, lines in groups:
        for i, chunk in enumerate(_chunk_lines(lines, _FIELD_LIMIT)):
            name = label if i == 0 else f"{label} (cont.)"
            used = _add_paged_field(
                embeds, discord.Embed(title=cont_title, color=discord.Color.blue()), used, name, chunk)
    return embeds


class KvkReport(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _compute(self, conn, event_id) -> bool:
        """Rank signups and auto-assign slots for every active type, honoring existing locks."""
        ev = kvkdb.get_event(conn, event_id)
        if not ev:
            return False
        slot_mode = ev["slot_mode"]
        full_day = len(generate_time_slots(slot_mode))
        for position_type in kvkdb.get_active_types(conn, event_id):
            signups = kvkdb.get_signups(conn, event_id, position_type)
            if ev["scope"] == "kingdom":
                locks = kvkdb.get_locks(conn, event_id, position_type, 0)
                kvkdb.save_slots(
                    conn, event_id, position_type, 0,
                    rank_and_assign(signups, full_day, slot_mode, locked=locks))
            else:
                by_alliance: dict = {}
                for s in signups:
                    aid_int = _alliance_id_of(s["fid"])
                    if aid_int is None:
                        continue  # orphan: no alliance, or non-numeric alliance id
                    by_alliance.setdefault(aid_int, []).append(s)
                n = ev["slots_per_alliance"] or 0
                for aid_int, group in by_alliance.items():
                    locks = kvkdb.get_locks(conn, event_id, position_type, aid_int)
                    kvkdb.save_slots(
                        conn, event_id, position_type, aid_int,
                        rank_and_assign(group, n, slot_mode, locked=locks))
        kvkdb.set_status(conn, event_id, "assigned")
        return True

    def _render_embeds(self, conn, event_id) -> list:
        """One embed per active position type, plus a notes embed for skipped signups."""
        ev = kvkdb.get_event(conn, event_id)
        if ev is None:
            return [discord.Embed(
                title="Unknown event", description=f"No event with id {event_id}.", color=discord.Color.red())]

        minutes_map = kvkdb.get_signup_minutes(conn, event_id)
        slots = kvkdb.get_slots(conn, event_id)
        nicknames = _nicknames_for({r["fid"] for r in slots if r["fid"] is not None})

        by_type: dict = {}
        for row in slots:
            by_type.setdefault(row["position_type"], []).append(row)

        embeds = []
        for position_type in kvkdb.get_active_types(conn, event_id):
            embeds.extend(_type_embeds(ev, position_type, by_type.get(position_type, []), minutes_map, nicknames))

        skipped = sorted(set(_skipped_fids(conn, event_id, ev)))
        if skipped:
            notes_title = "Report notes"
            notes_embeds = [discord.Embed(title=notes_title, color=discord.Color.orange())]
            used = len(notes_title)
            for chunk in _chunk_lines([str(fid) for fid in skipped], _FIELD_LIMIT):
                used = _add_paged_field(
                    notes_embeds, discord.Embed(title=notes_title, color=discord.Color.orange()),
                    used, "Skipped (no alliance)", chunk)
            embeds.extend(notes_embeds)
        return embeds

    @app_commands.command(name="kvk_report", description="Rank, assign, and preview KvK slots (Global Admin only).")
    @app_commands.describe(event_id="The KvK event ID")
    async def kvk_report(self, interaction: discord.Interaction, event_id: int):
        sched = self.bot.get_cog("KvkScheduling")
        if sched is None or not sched._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        if not self._compute(sched.conn, event_id):
            await interaction.response.send_message(f"Unknown event: {event_id}.", ephemeral=True)
            return

        ev = kvkdb.get_event(sched.conn, event_id)
        embeds = self._render_embeds(sched.conn, event_id)
        view = _OverrideView(self, sched.conn, event_id, ev["scope"])
        await interaction.response.send_message(
            embeds=embeds[:_MAX_EMBEDS_PER_MESSAGE], view=view, ephemeral=True)
        for extra in range(_MAX_EMBEDS_PER_MESSAGE, len(embeds), _MAX_EMBEDS_PER_MESSAGE):
            await interaction.followup.send(embeds=embeds[extra:extra + _MAX_EMBEDS_PER_MESSAGE], ephemeral=True)
        if view.truncated:
            await interaction.followup.send(
                f"Override select shows first {_MAX_GROUP_OPTIONS} of {len(view.groups)} groups.", ephemeral=True)
        if not view.groups:
            await interaction.followup.send("No groups to edit yet (no signups assigned).", ephemeral=True)


class _GroupSelect(discord.ui.Select):
    def __init__(self, override_view: "_OverrideView", groups: list):
        options = [
            discord.SelectOption(label=_group_label(override_view.scope, ptype, aid), value=f"{ptype}|{aid}")
            for ptype, aid in groups
        ]
        super().__init__(
            placeholder="Pick a group to edit (type + alliance)", options=options,
            min_values=1, max_values=1, row=0)
        self.override_view = override_view

    async def callback(self, interaction: discord.Interaction):
        position_type, aid = self.values[0].split("|")
        self.override_view.selected_type = position_type
        self.override_view.selected_alliance = int(aid)
        await interaction.response.send_message(
            f"Selected: {position_type}, alliance {aid}. Use Swap/Lock/Unlock/Clear next.", ephemeral=True)


class _SlotIndexModal(discord.ui.Modal):
    """One slot-index input, used by Lock, Unlock, and Clear."""

    def __init__(self, parent_view: "_OverrideView", title: str, on_confirm):
        super().__init__(title=title)
        self.parent_view = parent_view
        self.on_confirm = on_confirm
        self.slot_index = discord.ui.TextInput(label="Slot index", max_length=3)
        self.add_item(self.slot_index)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            index = int(self.slot_index.value.strip())
        except ValueError:
            await interaction.response.send_message("Slot index must be a whole number.", ephemeral=True)
            return
        await self.on_confirm(interaction, index)


class _SwapModal(discord.ui.Modal, title="Swap Slots"):
    def __init__(self, parent_view: "_OverrideView"):
        super().__init__()
        self.parent_view = parent_view
        self.index_a = discord.ui.TextInput(label="First slot index", max_length=3)
        self.add_item(self.index_a)
        self.index_b = discord.ui.TextInput(label="Second slot index", max_length=3)
        self.add_item(self.index_b)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            a = int(self.index_a.value.strip())
            b = int(self.index_b.value.strip())
        except ValueError:
            await interaction.response.send_message("Slot indices must be whole numbers.", ephemeral=True)
            return
        await self.parent_view.swap_slots(interaction, a, b)


class _OverrideView(discord.ui.View):
    """Admin controls to tweak an auto-assigned KvK report before publishing (Task 7)."""

    def __init__(self, report_cog: KvkReport, conn, event_id: int, scope: str):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.report_cog = report_cog
        self.conn = conn
        self.event_id = event_id
        self.scope = scope
        self.selected_type: str | None = None
        self.selected_alliance: int | None = None
        self.groups: list = []
        self._build_items()

    def _build_items(self) -> None:
        """(Re)build the view's components from the current group set.

        A Discord select must have 1-25 options, so the group select (and the buttons that
        depend on a selected group) are only added when at least one group exists. Called from
        __init__ and again from _refresh so the controls stay in sync after Re-run changes which
        groups exist (e.g. the first signup for a previously-empty alliance-scope event).
        """
        self.clear_items()
        self.groups = _distinct_groups(self.conn, self.event_id)
        if (self.selected_type, self.selected_alliance) not in self.groups:
            self.selected_type = None
            self.selected_alliance = None

        if self.groups:
            self.add_item(_GroupSelect(self, self.groups[:_MAX_GROUP_OPTIONS]))

            swap_button = discord.ui.Button(label="Swap", style=discord.ButtonStyle.secondary, row=1)
            swap_button.callback = self.swap_prompt
            self.add_item(swap_button)

            lock_button = discord.ui.Button(label="Lock", style=discord.ButtonStyle.secondary, row=1)
            lock_button.callback = self.lock_prompt
            self.add_item(lock_button)

            unlock_button = discord.ui.Button(label="Unlock", style=discord.ButtonStyle.secondary, row=1)
            unlock_button.callback = self.unlock_prompt
            self.add_item(unlock_button)

            clear_button = discord.ui.Button(label="Clear", style=discord.ButtonStyle.danger, row=2)
            clear_button.callback = self.clear_prompt
            self.add_item(clear_button)

        rerun_button = discord.ui.Button(label="Re-run", style=discord.ButtonStyle.primary, row=2)
        rerun_button.callback = self.rerun
        self.add_item(rerun_button)

        publish_button = discord.ui.Button(label="Publish", style=discord.ButtonStyle.success, row=2)
        publish_button.callback = self.publish_stub
        self.add_item(publish_button)

    @property
    def truncated(self) -> bool:
        return len(self.groups) > _MAX_GROUP_OPTIONS

    def _find_row(self, index: int):
        for row in kvkdb.get_slots(self.conn, self.event_id):
            if (row["position_type"] == self.selected_type
                    and row["alliance_id"] == self.selected_alliance
                    and row["slot_index"] == index):
                return row
        return None

    async def _require_group(self, interaction: discord.Interaction) -> bool:
        if self.selected_type is None:
            await interaction.response.send_message("Pick a group first.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction):
        self._build_items()
        embeds = self.report_cog._render_embeds(self.conn, self.event_id)
        await interaction.response.edit_message(embeds=embeds[:_MAX_EMBEDS_PER_MESSAGE], view=self)
        for extra in range(_MAX_EMBEDS_PER_MESSAGE, len(embeds), _MAX_EMBEDS_PER_MESSAGE):
            await interaction.followup.send(embeds=embeds[extra:extra + _MAX_EMBEDS_PER_MESSAGE], ephemeral=True)
        if not self.groups:
            await interaction.followup.send("No groups to edit yet (no signups assigned).", ephemeral=True)

    async def swap_prompt(self, interaction: discord.Interaction):
        if await self._require_group(interaction):
            await interaction.response.send_modal(_SwapModal(self))

    async def lock_prompt(self, interaction: discord.Interaction):
        if await self._require_group(interaction):
            await interaction.response.send_modal(_SlotIndexModal(self, "Lock Slot", self.lock_confirm))

    async def unlock_prompt(self, interaction: discord.Interaction):
        if await self._require_group(interaction):
            await interaction.response.send_modal(_SlotIndexModal(self, "Unlock Slot", self.unlock_confirm))

    async def clear_prompt(self, interaction: discord.Interaction):
        if await self._require_group(interaction):
            await interaction.response.send_modal(_SlotIndexModal(self, "Clear Slot", self.clear_slot))

    async def swap_slots(self, interaction: discord.Interaction, a: int, b: int):
        row_a = self._find_row(a)
        row_b = self._find_row(b)
        if row_a is None or row_b is None:
            await interaction.response.send_message(
                "One or both slot indices do not exist in this group.", ephemeral=True)
            return
        kvkdb.set_slot(
            self.conn, self.event_id, self.selected_type, self.selected_alliance, a, row_b["fid"], row_b["locked"])
        kvkdb.set_slot(
            self.conn, self.event_id, self.selected_type, self.selected_alliance, b, row_a["fid"], row_a["locked"])
        await self._refresh(interaction)

    async def toggle_lock(self, interaction: discord.Interaction, index: int, locked: int):
        row = self._find_row(index)
        if row is None:
            await interaction.response.send_message("Slot index not found in this group.", ephemeral=True)
            return
        if locked and row["fid"] is None:
            await interaction.response.send_message("Cannot lock an empty slot.", ephemeral=True)
            return
        kvkdb.set_slot(
            self.conn, self.event_id, self.selected_type, self.selected_alliance, index, row["fid"], locked)
        await self._refresh(interaction)

    async def lock_confirm(self, interaction: discord.Interaction, index: int):
        await self.toggle_lock(interaction, index, 1)

    async def unlock_confirm(self, interaction: discord.Interaction, index: int):
        await self.toggle_lock(interaction, index, 0)

    async def clear_slot(self, interaction: discord.Interaction, index: int):
        row = self._find_row(index)
        if row is None:
            await interaction.response.send_message("Slot index not found in this group.", ephemeral=True)
            return
        kvkdb.set_slot(self.conn, self.event_id, self.selected_type, self.selected_alliance, index, None, 0)
        await self._refresh(interaction)

    async def rerun(self, interaction: discord.Interaction):
        self.report_cog._compute(self.conn, self.event_id)
        await self._refresh(interaction)

    async def publish_stub(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Publish is not wired up yet. It will post the final assignments (Task 7).", ephemeral=True)


async def setup(bot):
    await bot.add_cog(KvkReport(bot))

"""KvK report: rank submissions, auto-assign slots, admin override, publish."""
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from . import kvk_alliances
from . import kvk_data as kvkdb
from .kvk_util import (
    CHIEF_TRAINING_BONUS,
    assign_two_tier,
    compute_training_points,
    format_speedups,
    generate_time_slots,
    rank_and_assign,
    troop_tier,
)
from .pimp_my_bot import theme

_ROLE_LABEL = {"noble": "Noble Advisor", "chief": "Chief Minister"}
_ROLE_ORDER = {"noble": 0, "chief": 1, "": 2}  # Noble seats listed first


def _chief_points(s) -> int:
    """A player's KvK points in a Chief seat (+10%) instead of the stored Noble value (+50%).

    Falls back to 0 (never the Noble value) if the inputs are missing/unreadable: a pro Training
    signup always has them, so a 0 here means malformed data, and inflating it to Noble would
    silently distort the seat assignment.
    """
    if not s.get("base_level"):
        return 0
    try:
        return compute_training_points(
            troop_tier(s["base_level"]), s["speedup_minutes"], s.get("upgrade_from"),
            s.get("upgrade_count") or 0, (s.get("training_speed") or 0) + CHIEF_TRAINING_BONUS,
        )["kvk_points"]
    except (ValueError, KeyError):
        return 0


def _training_rows(members, chief_slots, noble_slots, slot_mode, pro):
    """Noble + Chief seat rows for a Training group. Pro events place players to maximise total KvK
    points (Noble +50%, Chief +10%); standard events place by speedups. Noble seats take slot indices
    0..noble-1, Chief seats noble..noble+chief-1; each row carries its role and per-seat points."""
    times = generate_time_slots(slot_mode)
    if pro:
        players = [{**s, "noble_points": s.get("kvk_points") or 0, "chief_points": _chief_points(s)}
                   for s in members]
        noble, chief = assign_two_tier(players, noble_slots, chief_slots)
    else:
        ranked = sorted(members, key=lambda s: (-s["speedup_minutes"], s["submitted_at"], s["fid"]))
        noble, chief = ranked[:noble_slots], ranked[noble_slots:noble_slots + chief_slots]

    rows = []
    for i in range(noble_slots):
        p = noble[i] if i < len(noble) else None
        rows.append({
            "slot_index": i, "slot_time": times[i] if i < len(times) else "", "role": "noble",
            "fid": p["fid"] if p else None, "locked": 0,
            "points": p["noble_points"] if (p and pro) else None})
    for j in range(chief_slots):
        idx = noble_slots + j
        p = chief[j] if j < len(chief) else None
        rows.append({
            "slot_index": idx, "slot_time": times[idx] if idx < len(times) else "", "role": "chief",
            "fid": p["fid"] if p else None, "locked": 0,
            "points": p["chief_points"] if (p and pro) else None})
    return rows

# Position-type markers for the signups list.
_POSITION_ICON = {
    "Training": theme.trainingIcon, "Research": theme.researchIcon, "Building": theme.constructionIcon}

VIEW_TIMEOUT = 7200
_FIELD_LIMIT = 1024
_EMBED_CHAR_BUDGET = 5500  # Discord caps an embed at 6000 chars total; leave headroom for the title
_MAX_FIELDS_PER_EMBED = 25  # Discord's hard field-count cap per embed
_MAX_EMBEDS_PER_MESSAGE = 10
_MAX_GROUP_OPTIONS = 25
_LOCK_MARK = "[LOCKED]"
_LAYOUT_CHILD_CAP = 34   # keep each LayoutView under Discord's 40-children limit (container + its items)
_TEXT_CHUNK = 3500       # max chars per TextDisplay; leaves room for the header under the message budget
_MSG_CHAR_BUDGET = 3800  # Discord caps a Components-v2 message at 4000 display chars across all text
_TYPE_COLOURS = [discord.Color.blurple(), discord.Color.green(), discord.Color.gold()]


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


def _seat_points(conn, event_id, ev) -> dict:
    """(fid, role) -> Training seat KvK points for a Pro event: Noble uses the stored (+50%) value,
    Chief is recomputed at +10%. Empty for non-pro events. Keyed by (fid, role) and computed from
    signups (not the slot row) so a manual Swap - which moves a fid to a different seat - shows the
    right number without a Re-run."""
    if not ev["pro_mode"]:
        return {}
    points: dict = {}
    for s in kvkdb.get_signups(conn, event_id, "Training"):
        points[(s["fid"], "noble")] = s.get("kvk_points") or 0
        points[(s["fid"], "chief")] = _chief_points(s)
    return points


def _alliance_names() -> dict:
    """alliance_id -> name for every alliance the bot knows (for schedule labels)."""
    return dict(kvk_alliances.list_alliances())


def _alliance_label(alliance_id: int, names: dict) -> str:
    return names.get(alliance_id, f"Alliance {alliance_id}")


def _group_label(free_mode, position_type: str, alliance_id: int, names: dict) -> str:
    if free_mode:
        return f"{position_type} - Kingdom"
    return f"{position_type} - {_alliance_label(alliance_id, names)}"


def _distinct_groups(conn, event_id) -> list:
    """Distinct (position_type, alliance_id) pairs that have slot rows, in query order."""
    seen: dict = {}
    for row in kvkdb.get_slots(conn, event_id):
        key = (row["position_type"], row["alliance_id"])
        seen.setdefault(key, None)
    return list(seen)


def _skipped_fids(conn, event_id, ev: dict) -> list:
    """Signed-up fids that cannot be placed in an alliance-based event: their alliance is not one of
    the event's alliances (e.g. it was removed after they signed up, or they became unbound).

    Recomputed fresh from current signup data on every call (not cached on the cog), so it is
    always scoped to the one event being rendered and stays correct after Swap/Lock/Unlock/Clear
    refreshes that do not re-run _compute. Free-mode events place everyone, so none are skipped.
    """
    if ev["free_mode"]:
        return []
    added = {a["alliance_id"] for a in kvkdb.get_event_alliances(conn, event_id)}
    skipped = []
    for position_type in kvkdb.get_active_types(conn, event_id):
        for s in kvkdb.get_signups(conn, event_id, position_type):
            if kvk_alliances.alliance_id_of(s["fid"]) not in added:
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


def _metric_map(conn, event_id, ev) -> dict:
    """Per (fid, position_type) metric shown on a slot line: KvK points for Pro training, else speedups."""
    metrics: dict = {}
    for position_type in kvkdb.get_active_types(conn, event_id):
        pro_training = bool(ev["pro_mode"]) and position_type == "Training"
        for s in kvkdb.get_signups(conn, event_id, position_type):
            metrics[(s["fid"], position_type)] = (
                f"{(s['kvk_points'] or 0):,} pts" if pro_training else _fmt_minutes(s["speedup_minutes"]))
    return metrics


def _slot_line(row: dict, position_type: str, metric_map: dict, nicknames: dict, seat_points: dict) -> str:
    if row["fid"] is None:
        return f"{row['slot_time']} - (empty)"
    nickname = nicknames.get(row["fid"], f"Unknown ({row['fid']})")
    # A Pro training seat's points depend on its role (Noble +50% vs Chief +10%) and current occupant,
    # looked up by (fid, role) so a Swap stays correct; otherwise use the shared metric (speedups).
    role = row.get("role", "")
    if role and (row["fid"], role) in seat_points:
        metric = f"{seat_points[(row['fid'], role)]:,} pts"
    else:
        metric = metric_map.get((row["fid"], position_type), "?")
    lock = f" {_LOCK_MARK}" if row["locked"] else ""
    return f"{row['slot_time']} - {nickname} ({row['fid']}) - {metric}{lock}"


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


def _type_embeds(ev: dict, position_type: str, rows: list, metric_map: dict, nicknames: dict,
                 names: dict, seat_points: dict) -> list:
    """One or more embeds for a single position type, paged under Discord's 6000-char embed cap."""
    groups: list = []
    if ev["free_mode"]:
        lines = [_slot_line(r, position_type, metric_map, nicknames, seat_points) for r in rows]
        groups.append(("Kingdom", lines))
    else:
        by_group: dict = {}  # (alliance_id, role) -> rows; role splits Training into Noble/Chief
        for r in rows:
            by_group.setdefault((r["alliance_id"], r.get("role", "")), []).append(r)
        for aid, role in sorted(by_group, key=lambda k: (k[0], _ROLE_ORDER.get(k[1], 9))):
            label = _alliance_label(aid, names)
            if role:
                label += f" - {_ROLE_LABEL.get(role, role)}"
            lines = [_slot_line(r, position_type, metric_map, nicknames, seat_points) for r in by_group[(aid, role)]]
            groups.append((label, lines))

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


def _fmt_desired(indices, times) -> str:
    """Compact preferred-times suffix for a signup line, e.g. ' - any 1 of 5 slots (20:00-22:00)'.

    The player marks slots they can take; the leader gives them one. So the wording says "any 1 of",
    not "wants N", which would read as a request for that many slots."""
    picks = [times[i] for i in indices if 0 <= i < len(times)]
    if not picks:
        return ""
    if len(picks) == 1:
        return " - prefers " + picks[0]
    if len(picks) <= 4:
        return " - any 1 of " + ", ".join(picks)
    return f" - any 1 of {len(picks)} slots ({picks[0]}-{picks[-1]})"


def _signups_embeds(conn, event_id) -> list:
    """Every raw signup for an event, ranked per position type. For the menu View button."""
    ev = kvkdb.get_event(conn, event_id)
    if ev is None:
        return [discord.Embed(
            title="Unknown event", description=f"No event with id {event_id}.", color=discord.Color.red())]
    grid = generate_time_slots(ev["slot_mode"])
    active_types = kvkdb.get_active_types(conn, event_id)
    per_type: dict = {}
    all_fids: set = set()
    for position_type in active_types:
        pro_training = bool(ev["pro_mode"]) and position_type == "Training"
        ranked = sorted(
            kvkdb.get_signups(conn, event_id, position_type),
            key=lambda s, _p=pro_training: (
                -((s["kvk_points"] or 0) if _p else s["speedup_minutes"]), s["submitted_at"], s["fid"]))
        per_type[position_type] = ranked
        all_fids.update(s["fid"] for s in ranked)
    nicknames = _nicknames_for(all_fids)

    title = f"{theme.crossIcon} {ev['name']} - Signups"
    cont_title = f"{ev['name']} - Signups (cont.)"
    description = f"{len(all_fids)} player(s) signed up. Ranked per type (KvK points for Pro training)."
    embeds = [discord.Embed(title=title, description=description, color=discord.Color.blue())]
    used = len(title) + len(description)
    for position_type in active_types:
        pro_training = bool(ev["pro_mode"]) and position_type == "Training"
        signups = per_type[position_type]
        lines = []
        for rank, s in enumerate(signups, 1):
            nick = nicknames.get(s["fid"], "Unknown")  # the fid is shown separately below
            pref = _fmt_desired(s.get("desired_slots", []), grid)
            metric = f"{(s['kvk_points'] or 0):,} pts" if pro_training else format_speedups(s["speedup_minutes"])
            lines.append(f"{rank}. {nick} ({s['fid']}) - **{metric}**{pref}")
        if not lines:
            lines = ["(no signups)"]
        icon = _POSITION_ICON.get(position_type, "")
        for i, chunk in enumerate(_chunk_lines(lines, _FIELD_LIMIT)):
            name = f"{icon} {position_type} ({len(signups)})" if i == 0 else f"{position_type} (cont.)"
            used = _add_paged_field(
                embeds, discord.Embed(title=cont_title, color=discord.Color.blue()), used, name, chunk)
    return embeds


class _ScheduleLayout(discord.ui.LayoutView):
    """A single Components-v2 message: one accent-coloured container of schedule text."""

    def __init__(self, container: discord.ui.Container):
        super().__init__(timeout=None)
        self.add_item(container)


def _emit_layouts(layouts: list, title: str, blocks: list, colour) -> None:
    """Pack a title plus blocks into one or more _ScheduleLayout messages.

    blocks are ("text", str) or ("sep", "") pairs. Each message stays under BOTH Discord's
    40-children limit (via _LAYOUT_CHILD_CAP) and its 4000-char text budget (via _MSG_CHAR_BUDGET,
    which discord.py does not enforce); overflow spills into a "(cont.)" message.
    """
    idx = 0
    part = 0
    while True:
        header = title if part == 0 else f"{title} (cont.)"
        children = [discord.ui.TextDisplay(header)]
        used = len(header)
        while idx < len(blocks) and len(children) < _LAYOUT_CHILD_CAP:
            kind, text = blocks[idx]
            if kind == "sep" and len(children) == 1:
                idx += 1  # drop a divider that would lead a fresh message
                continue
            # Always place at least one real block (len(children) == 1), else respect the char budget.
            if len(children) > 1 and used + len(text) > _MSG_CHAR_BUDGET:
                break
            children.append(discord.ui.Separator() if kind == "sep" else discord.ui.TextDisplay(text))
            used += len(text)
            idx += 1
        layouts.append(_ScheduleLayout(discord.ui.Container(*children, accent_colour=colour)))
        part += 1
        if idx >= len(blocks):
            break


def _render_layouts(conn, event_id) -> list:
    """Build the published schedule as Components-v2 LayoutViews (one or more messages).

    One accent colour per position type; free mode is one text block, alliance mode is one block
    per alliance separated by a divider. Skipped signups get a final orange note.
    """
    ev = kvkdb.get_event(conn, event_id)
    if ev is None:
        container = discord.ui.Container(
            discord.ui.TextDisplay(f"No event with id {event_id}."), accent_colour=discord.Color.red())
        return [_ScheduleLayout(container)]

    metric_map = _metric_map(conn, event_id, ev)
    seat_points = _seat_points(conn, event_id, ev)
    slots = kvkdb.get_slots(conn, event_id)
    nicknames = _nicknames_for({r["fid"] for r in slots if r["fid"] is not None})
    names = _alliance_names()
    by_type: dict = {}
    for row in slots:
        by_type.setdefault(row["position_type"], []).append(row)

    layouts: list = []
    for i, position_type in enumerate(kvkdb.get_active_types(conn, event_id)):
        colour = _TYPE_COLOURS[i % len(_TYPE_COLOURS)]
        rows = by_type.get(position_type, [])
        blocks: list = []
        if ev["free_mode"]:
            lines = [_slot_line(r, position_type, metric_map, nicknames, seat_points) for r in rows]
            blocks.extend(("text", chunk) for chunk in _chunk_lines(lines, _TEXT_CHUNK))
        else:
            by_group: dict = {}  # (alliance_id, role) -> rows; role splits Training into Noble/Chief
            for r in rows:
                by_group.setdefault((r["alliance_id"], r.get("role", "")), []).append(r)
            ordered = sorted(by_group, key=lambda k: (k[0], _ROLE_ORDER.get(k[1], 9)))
            for pos, (aid, role) in enumerate(ordered):
                if pos:  # a divider before every section after the first
                    blocks.append(("sep", ""))
                heading = _alliance_label(aid, names)
                if role:
                    heading += f" - {_ROLE_LABEL.get(role, role)}"
                lines = [_slot_line(r, position_type, metric_map, nicknames, seat_points)
                         for r in by_group[(aid, role)]]
                chunks = _chunk_lines(lines, _TEXT_CHUNK)
                blocks.append(("text", f"**{heading}**\n{chunks[0]}"))
                blocks.extend(("text", chunk) for chunk in chunks[1:])
        _emit_layouts(layouts, f"## {ev['name']} - {position_type}", blocks, colour)

    skipped = sorted(set(_skipped_fids(conn, event_id, ev)))
    if skipped:
        blocks = [("text", chunk) for chunk in _chunk_lines([str(f) for f in skipped], _TEXT_CHUNK)]
        _emit_layouts(layouts, "## Report notes - Skipped (no alliance)", blocks, discord.Color.orange())
    return layouts


class KvkReport(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _assignment_rows(self, conn, event_id) -> dict:
        """Rank and place every group in memory, honoring existing locks. Writes nothing.

        Returns {(position_type, alliance_id): [slot rows]}. Shared by _compute (which persists and
        closes signups) and _preview_embeds (which renders without persisting or closing signups).
        """
        ev = kvkdb.get_event(conn, event_id)
        if ev is None:  # event deleted while a confirm/preview view was still open
            return {}
        slot_mode = ev["slot_mode"]
        full_day = len(generate_time_slots(slot_mode))
        groups: dict = {}
        for position_type in kvkdb.get_active_types(conn, event_id):
            signups = kvkdb.get_signups(conn, event_id, position_type)
            pro_training = bool(ev["pro_mode"]) and position_type == "Training"
            if ev["free_mode"]:
                if pro_training:
                    for s in signups:  # kingdom pool ranks by computed KvK points, not raw speedups
                        s["score"] = s["kvk_points"] or 0
                locks = kvkdb.get_locks(conn, event_id, position_type, 0)
                groups[(position_type, 0)] = rank_and_assign(signups, full_day, slot_mode, locked=locks)
            else:
                by_alliance: dict = {}
                for s in signups:
                    aid_int = kvk_alliances.alliance_id_of(s["fid"])
                    if aid_int is None:
                        continue  # unbound: cannot place (the signup gate should have blocked it)
                    by_alliance.setdefault(aid_int, []).append(s)
                # Each of the event's alliances gets its own seats; non-added alliances are ignored
                # here and reported by _skipped_fids.
                for a in kvkdb.get_event_alliances(conn, event_id):
                    aid_int = a["alliance_id"]
                    members = by_alliance.get(aid_int, [])
                    if position_type == "Training":
                        # Training day = Noble Advisor + Chief Minister seats.
                        groups[(position_type, aid_int)] = _training_rows(
                            members, a["chief_slots"], a["noble_slots"], slot_mode, pro_training)
                    else:
                        # Building/Research = Chief Minister seats only.
                        locks = kvkdb.get_locks(conn, event_id, position_type, aid_int)
                        groups[(position_type, aid_int)] = rank_and_assign(
                            members, a["chief_slots"], slot_mode, locked=locks)
        return groups

    def _compute(self, conn, event_id) -> bool:
        """Rank signups and auto-assign slots for every active type, honoring existing locks."""
        if kvkdb.get_event(conn, event_id) is None:
            return False
        for (position_type, aid), rows in self._assignment_rows(conn, event_id).items():
            kvkdb.save_slots(conn, event_id, position_type, aid, rows)
        kvkdb.set_status(conn, event_id, "assigned")
        return True

    def _preview_embeds(self, conn, event_id) -> list:
        """Render what Report / Assign would produce, without saving or closing signups (dry run)."""
        slots = []
        for (position_type, aid), rows in self._assignment_rows(conn, event_id).items():
            for r in rows:
                slots.append({
                    "position_type": position_type, "alliance_id": aid, "slot_index": r["slot_index"],
                    "slot_time": r["slot_time"], "fid": r["fid"], "locked": r["locked"],
                    "role": r.get("role", ""), "points": r.get("points")})
        return self._render_embeds(conn, event_id, slots=slots)

    def _render_embeds(self, conn, event_id, slots=None) -> list:
        """One embed per active position type, plus a notes embed for skipped signups.

        Pass `slots` to render a set of rows that is not (yet) in the database, e.g. a preview.
        """
        ev = kvkdb.get_event(conn, event_id)
        if ev is None:
            return [discord.Embed(
                title="Unknown event", description=f"No event with id {event_id}.", color=discord.Color.red())]

        metric_map = _metric_map(conn, event_id, ev)
        seat_points = _seat_points(conn, event_id, ev)
        if slots is None:
            slots = kvkdb.get_slots(conn, event_id)
        nicknames = _nicknames_for({r["fid"] for r in slots if r["fid"] is not None})

        by_type: dict = {}
        for row in slots:
            by_type.setdefault(row["position_type"], []).append(row)

        names = _alliance_names()
        embeds = []
        for position_type in kvkdb.get_active_types(conn, event_id):
            embeds.extend(_type_embeds(
                ev, position_type, by_type.get(position_type, []), metric_map, nicknames, names,
                seat_points))

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

    async def _confirm_publish(self, interaction: discord.Interaction, conn, event_id: int) -> None:
        """Ask the admin to confirm before posting to the public channel, so a stray click cannot.

        Shared by /kvk_publish, the /settings menu, and the _OverrideView Publish button.
        """
        ev = kvkdb.get_event(conn, event_id)
        if not ev or ev["status"] not in ("assigned", "published"):
            await interaction.response.send_message(
                "Run /kvk_report (or the Report / Assign button) first.", ephemeral=True)
            return
        if not ev["publish_channel_id"]:
            await interaction.response.send_message("No publish channel set.", ephemeral=True)
            return
        again = " again" if ev["status"] == "published" else ""
        message = (
            f"Publish '{ev['name']}' to <#{ev['publish_channel_id']}>{again}?\n\n"
            f"What this does:\n"
            f"- posts the full schedule to that channel (everyone there can see it)\n"
            f"- marks the event as \"published\"\n\n"
            f"Signups for this event are already closed at this stage - they close when you run "
            f"Report / Assign, not here - and publishing does not reopen them."
        )
        await interaction.response.send_message(
            message, view=_ConfirmPublishView(self, conn, event_id), ephemeral=True)

    async def _do_publish(self, interaction: discord.Interaction, conn, event_id: int) -> None:
        """Post the report to the publish channel and mark it published.

        Called from the confirm view after it has edited (acked) the interaction, so it uses followups.
        """
        ev = kvkdb.get_event(conn, event_id)
        if not ev or ev["status"] not in ("assigned", "published"):
            await interaction.followup.send(
                "Run /kvk_report (or the Report / Assign button) first.", ephemeral=True)
            return
        if not ev["publish_channel_id"]:
            await interaction.followup.send("No publish channel set.", ephemeral=True)
            return
        channel = self.bot.get_channel(int(ev["publish_channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(ev["publish_channel_id"]))
            except (discord.Forbidden, discord.NotFound):
                await interaction.followup.send("Publish channel not found.", ephemeral=True)
                return
        for layout in _render_layouts(conn, event_id):
            await channel.send(view=layout)
        kvkdb.set_status(conn, event_id, "published")
        await interaction.followup.send("Published.", ephemeral=True)

    async def launch_report(self, interaction: discord.Interaction, event_id: int) -> None:
        """Confirm the consequences, then rank + assign. Shared by /kvk_report and the KvK menu."""
        sched = self.bot.get_cog("KvkScheduling")
        if sched is None or not sched._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        ev = kvkdb.get_event(sched.conn, event_id)
        if ev is None:
            await interaction.response.send_message(f"Unknown event: {event_id}.", ephemeral=True)
            return
        if ev["status"] == "collecting":
            gate_line = ('- CLOSES signups: the event moves from "collecting" to "assigned", '
                         'so /kvk_signup stops accepting entries for it\n')
        else:
            gate_line = f"- signups are already closed (status: {ev['status']})\n"
        message = (
            f"Run Report / Assign for '{ev['name']}'?\n\n"
            f"What this does:\n"
            f"- ranks every signup (by speedups, or KvK points in Pro mode) and fills the slots\n"
            f"{gate_line}"
            f"- opens the override panel (swap / lock / clear / publish)\n\n"
            f"You can re-run it from that panel; signups do not reopen."
        )
        await interaction.response.send_message(
            message, view=_ConfirmReportView(self, event_id), ephemeral=True)

    async def _run_report(self, interaction: discord.Interaction, event_id: int, *, edit: bool) -> None:
        """Compute the ranking and show the override view. `edit` replaces the confirm message in place."""
        sched = self.bot.get_cog("KvkScheduling")
        if sched is None or not self._compute(sched.conn, event_id):
            await interaction.response.send_message(f"Unknown event: {event_id}.", ephemeral=True)
            return
        ev = kvkdb.get_event(sched.conn, event_id)
        embeds = self._render_embeds(sched.conn, event_id)
        view = _OverrideView(self, sched.conn, event_id, ev["free_mode"])
        first = embeds[:_MAX_EMBEDS_PER_MESSAGE]
        if edit:
            await interaction.response.edit_message(content=None, embeds=first, view=view)
        else:
            await interaction.response.send_message(embeds=first, view=view, ephemeral=True)
        for extra in range(_MAX_EMBEDS_PER_MESSAGE, len(embeds), _MAX_EMBEDS_PER_MESSAGE):
            await interaction.followup.send(embeds=embeds[extra:extra + _MAX_EMBEDS_PER_MESSAGE], ephemeral=True)
        if view.truncated:
            await interaction.followup.send(
                f"Override select shows first {_MAX_GROUP_OPTIONS} of {len(view.groups)} groups.", ephemeral=True)
        if not view.groups:
            await interaction.followup.send("No groups to edit yet (no signups assigned).", ephemeral=True)

    async def launch_view_signups(self, interaction: discord.Interaction, event_id: int) -> None:
        """Show every raw signup for an event, ranked. Used by the /settings KvK menu View button."""
        sched = self.bot.get_cog("KvkScheduling")
        if sched is None or not sched._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        if kvkdb.get_event(sched.conn, event_id) is None:
            await interaction.response.send_message(f"Unknown event: {event_id}.", ephemeral=True)
            return
        embeds = _signups_embeds(sched.conn, event_id)
        await interaction.response.send_message(embeds=embeds[:_MAX_EMBEDS_PER_MESSAGE], ephemeral=True)
        for extra in range(_MAX_EMBEDS_PER_MESSAGE, len(embeds), _MAX_EMBEDS_PER_MESSAGE):
            await interaction.followup.send(embeds=embeds[extra:extra + _MAX_EMBEDS_PER_MESSAGE], ephemeral=True)

    async def launch_publish(self, interaction: discord.Interaction, event_id: int) -> None:
        """Publish the schedule. Shared by /kvk_publish and the /settings KvK menu."""
        sched = self.bot.get_cog("KvkScheduling")
        if sched is None or not sched._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        await self._confirm_publish(interaction, sched.conn, event_id)

    @app_commands.command(name="kvk_report", description="Rank, assign, and preview KvK slots (Global Admin only).")
    @app_commands.describe(event_id="The KvK event ID")
    async def kvk_report(self, interaction: discord.Interaction, event_id: int):
        await self.launch_report(interaction, event_id)

    @app_commands.command(name="kvk_publish", description="Publish the KvK schedule (Global Admin only).")
    @app_commands.describe(event_id="The KvK event ID")
    async def kvk_publish(self, interaction: discord.Interaction, event_id: int):
        await self.launch_publish(interaction, event_id)


class _GroupSelect(discord.ui.Select):
    def __init__(self, override_view: "_OverrideView", groups: list):
        options = [
            discord.SelectOption(
                label=_group_label(override_view.free_mode, ptype, aid, override_view.names),
                value=f"{ptype}|{aid}",
                default=(ptype == override_view.selected_type
                         and aid == override_view.selected_alliance))  # stay shown after a refresh
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

    def __init__(self, report_cog: KvkReport, conn, event_id: int, free_mode: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.report_cog = report_cog
        self.conn = conn
        self.event_id = event_id
        self.free_mode = free_mode
        self.names = _alliance_names()
        self.selected_type: str | None = None
        self.selected_alliance: int | None = None
        self.groups: list = []
        self._build_items()

    def _build_items(self) -> None:
        """(Re)build the view's components from the current group set.

        A Discord select must have 1-25 options, so the group select (and the buttons that
        depend on a selected group) are only added when at least one group exists. Called from
        __init__ and again from _refresh so the controls stay in sync after Re-run changes which
        groups exist (e.g. the first signup for a previously-empty alliance).
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
        publish_button.callback = self.publish
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

    async def publish(self, interaction: discord.Interaction):
        await self.report_cog._confirm_publish(interaction, self.conn, self.event_id)


class _ConfirmReportView(discord.ui.View):
    """A Yes/Cancel gate before Report / Assign, which closes signups on the first run."""

    def __init__(self, report_cog: KvkReport, event_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.report_cog = report_cog
        self.event_id = event_id
        yes_button = discord.ui.Button(label="Yes, run it", style=discord.ButtonStyle.primary)
        yes_button.callback = self.confirm
        self.add_item(yes_button)
        preview_button = discord.ui.Button(label="Preview", style=discord.ButtonStyle.secondary)
        preview_button.callback = self.preview
        self.add_item(preview_button)
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    async def confirm(self, interaction: discord.Interaction):
        await self.report_cog._run_report(interaction, self.event_id, edit=True)

    async def preview(self, interaction: discord.Interaction):
        sched = self.report_cog.bot.get_cog("KvkScheduling")
        if sched is None:
            await interaction.response.send_message("KvK Scheduling module not found.", ephemeral=True)
            return
        embeds = self.report_cog._preview_embeds(sched.conn, self.event_id)
        await interaction.response.send_message(
            content="Preview only - nothing saved, signups stay open. Run it to commit.",
            embeds=embeds[:_MAX_EMBEDS_PER_MESSAGE], ephemeral=True)
        for extra in range(_MAX_EMBEDS_PER_MESSAGE, len(embeds), _MAX_EMBEDS_PER_MESSAGE):
            await interaction.followup.send(embeds=embeds[extra:extra + _MAX_EMBEDS_PER_MESSAGE], ephemeral=True)

    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Report cancelled.", view=None)


class _ConfirmPublishView(discord.ui.View):
    """A Yes/Cancel gate shown before the public publish, so a stray click cannot post the schedule."""

    def __init__(self, report_cog: KvkReport, conn, event_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.report_cog = report_cog
        self.conn = conn
        self.event_id = event_id
        yes_button = discord.ui.Button(label="Yes, publish", style=discord.ButtonStyle.success)
        yes_button.callback = self.confirm
        self.add_item(yes_button)
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    async def confirm(self, interaction: discord.Interaction):
        # Edit first: this acks the click and drops the buttons so a second click cannot re-post.
        await interaction.response.edit_message(content="Publishing...", view=None)
        await self.report_cog._do_publish(interaction, self.conn, self.event_id)

    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Publish cancelled.", view=None)


async def setup(bot):
    await bot.add_cog(KvkReport(bot))

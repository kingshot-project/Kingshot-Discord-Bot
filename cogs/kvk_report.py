"""KvK report: rank submissions, auto-assign slots, admin override."""
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from . import kvk_alliances
from . import kvk_data as kvkdb
from .kvk_util import (
    CHIEF_TRAINING_BONUS,
    assign_alliance_slots,
    assign_two_tier,
    compute_training_points,
    format_speedups,
    generate_time_slots,
    parse_desired_slots,
    place_on_grid,
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


def _locks_by_grid(conn, event_id, position_type, alliance_id, slot_mode, role=None):
    """Locked slots of a group as {grid_index: fid}, read from the locked rows' slot_time.

    In alliance mode slot_index is opaque and slot_time is the real time, so a lock must re-pin by
    TIME on Re-run (a moved-then-locked player keeps their new time). Filter by role to split a
    Training group's Noble and Chief locks."""
    grid_pos = {t: i for i, t in enumerate(generate_time_slots(slot_mode))}
    out = {}
    for r in kvkdb.get_slots(conn, event_id):
        if (r["position_type"] == position_type and r["alliance_id"] == alliance_id
                and r["locked"] and r["fid"] is not None
                and (role is None or r.get("role", "") == role)):
            gi = grid_pos.get(r["slot_time"])
            if gi is not None:
                out[gi] = r["fid"]
    return out


def _training_rows(members, chief_slots, noble_slots, slot_mode, pro, locked_noble=None, locked_chief=None):
    """Noble + Chief seat rows for a Training group, each player placed at their preferred day time.
    Pro events pick Noble/Chief membership to maximise total KvK points (Noble +50%, Chief +10%);
    standard events split by speedups. Returns only the occupied rows; each carries its role and, for
    pro events, its per-seat points.

    locked_noble / locked_chief are {grid_index: fid}. A locked player keeps their role and time: they
    are pre-placed and consume a seat of that role, and the two-tier / speedup split fills the rest."""
    locked_noble = dict(locked_noble or {})
    locked_chief = dict(locked_chief or {})
    if pro:
        enriched = {s["fid"]: {**s, "noble_points": s.get("kvk_points") or 0, "chief_points": _chief_points(s)}
                    for s in members}
    else:
        enriched = {s["fid"]: s for s in members}

    lock_noble_fids = [f for f in locked_noble.values() if f in enriched]
    lock_chief_fids = [f for f in locked_chief.values() if f in enriched]
    locked_all = set(lock_noble_fids) | set(lock_chief_fids)
    free = [enriched[s["fid"]] for s in members if s["fid"] not in locked_all]
    noble_room = max(0, noble_slots - len(lock_noble_fids))
    chief_room = max(0, chief_slots - len(lock_chief_fids))
    if pro:
        noble_free, chief_free = assign_two_tier(free, noble_room, chief_room)
    else:
        ranked = sorted(free, key=lambda s: (-s["speedup_minutes"], s["submitted_at"], s["fid"]))
        noble_free, chief_free = ranked[:noble_room], ranked[noble_room:noble_room + chief_room]

    noble_group = [enriched[f] for f in lock_noble_fids] + noble_free
    chief_group = [enriched[f] for f in lock_chief_fids] + chief_free

    rows = []
    for role, group, pts_key, locked in (
        ("noble", noble_group, "noble_points", locked_noble),
        ("chief", chief_group, "chief_points", locked_chief),
    ):
        # Rank the within-role placement by seat points (Pro) so a higher-value player gets first pick
        # of a contested preferred time; standard events fall back to speedups inside place_on_grid.
        scored = [{**p, "score": p[pts_key]} for p in group] if pro else group
        points_by_fid = {p["fid"]: p.get(pts_key) for p in group} if pro else {}
        group_fids = {p["fid"] for p in group}
        role_lock = {g: f for g, f in locked.items() if f in group_fids}
        for r in place_on_grid(scored, slot_mode, locked=role_lock):
            rows.append({**r, "role": role, "points": points_by_fid.get(r["fid"])})
    # Noble and Chief are placed on the same day grid, so their real-time slot indices can collide.
    # The kvk_slots key is (event, position_type, alliance_id, slot_index) with no role, so give the
    # combined group stable sequential indices; the real time stays in slot_time for display.
    for i, r in enumerate(rows):
        r["slot_index"] = i
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
                 names: dict, seat_points: dict, alliance_seats: dict) -> list:
    """One or more embeds for a single position type, paged under Discord's 6000-char embed cap.

    Alliance groups list only the occupied seats at each player's real preferred time and end with a
    "(K seats open)" line; the header shows "(N seats, M filled)". Free mode shows the full grid."""
    groups: list = []
    # slot_index is opaque in alliance mode, so order rows by their real time (from slot_time).
    grid_pos = {t: i for i, t in enumerate(generate_time_slots(ev["slot_mode"]))}
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
            # Noble seats count against noble_slots; Chief seats and role-less (Research/Building) rows
            # count against chief_slots.
            seats = alliance_seats.get(aid, {}).get("noble" if role == "noble" else "chief", 0)
            occupied = sorted(
                (r for r in by_group[(aid, role)] if r["fid"] is not None),
                key=lambda r: grid_pos.get(r["slot_time"], 0))
            if seats:
                label += f" ({seats} seats, {len(occupied)} filled)"
            lines = [_slot_line(r, position_type, metric_map, nicknames, seat_points) for r in occupied]
            open_seats = max(0, seats - len(occupied))
            if open_seats:
                lines.append(f"({open_seats} seats open)")
            if not lines:
                lines = ["(no one placed)"]
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
                    # Locks re-pin by TIME (grid index from slot_time), since slot_index is opaque here.
                    if position_type == "Training":
                        # Training day = Noble Advisor + Chief Minister seats.
                        lock_n = _locks_by_grid(conn, event_id, position_type, aid_int, slot_mode, "noble")
                        lock_c = _locks_by_grid(conn, event_id, position_type, aid_int, slot_mode, "chief")
                        groups[(position_type, aid_int)] = _training_rows(
                            members, a["chief_slots"], a["noble_slots"], slot_mode, pro_training, lock_n, lock_c)
                    else:
                        # Building/Research = Chief Minister seats only; place at preferred times.
                        locks = _locks_by_grid(conn, event_id, position_type, aid_int, slot_mode)
                        groups[(position_type, aid_int)] = assign_alliance_slots(
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
        alliance_seats = {} if ev["free_mode"] else {
            a["alliance_id"]: {"chief": a["chief_slots"], "noble": a["noble_slots"]}
            for a in kvkdb.get_event_alliances(conn, event_id)}
        embeds = []
        for position_type in kvkdb.get_active_types(conn, event_id):
            embeds.extend(_type_embeds(
                ev, position_type, by_type.get(position_type, []), metric_map, nicknames, names,
                seat_points, alliance_seats))

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
            f"- opens the override panel (pick a player, then lock / unlock / move / remove)\n\n"
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

    @app_commands.command(name="kvk_report", description="Rank, assign, and preview KvK slots (Global Admin only).")
    @app_commands.describe(event_id="The KvK event ID")
    async def kvk_report(self, interaction: discord.Interaction, event_id: int):
        await self.launch_report(interaction, event_id)


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
        self.override_view.selected_index = None
        self.override_view.player_page = 0
        await self.override_view._refresh(interaction)


class _PlayerSelect(discord.ui.Select):
    """Pick a player in the chosen group (paginated, 25 per page). Lock/Unlock/Move/Remove act on it."""

    def __init__(self, override_view: "_OverrideView", page_rows: list, page: int, total_pages: int):
        options = []
        for r in page_rows:
            role = r.get("role", "")
            role_txt = f", {_ROLE_LABEL[role]}" if role in _ROLE_LABEL else ""
            lock_txt = " [LOCKED]" if r["locked"] else ""
            nick = override_view.nicknames.get(r["fid"], f"Unknown ({r['fid']})")
            options.append(discord.SelectOption(
                label=f"{r['slot_time']} - {nick} ({r['fid']}){role_txt}{lock_txt}"[:100],
                value=str(r["slot_index"]),
                default=(r["slot_index"] == override_view.selected_index)))
        super().__init__(
            placeholder=f"Pick a player (page {page + 1}/{total_pages})", options=options,
            min_values=1, max_values=1, row=1)
        self.override_view = override_view

    async def callback(self, interaction: discord.Interaction):
        self.override_view.selected_index = int(self.values[0])
        await self.override_view._refresh(interaction)


class _MoveTimeModal(discord.ui.Modal, title="Move to time"):
    def __init__(self, parent_view: "_OverrideView"):
        super().__init__()
        self.parent_view = parent_view
        self.time_input = discord.ui.TextInput(label="Time (e.g. 15:00)", max_length=11)
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.parent_view.do_move(interaction, self.time_input.value)


class _OverrideView(discord.ui.View):
    """Admin controls to tweak an auto-assigned KvK report: pick a group, pick a player, then
    Lock / Unlock / Move / Remove - no slot-index typing, the player select carries the times."""

    PLAYER_PAGE_SIZE = 25

    def __init__(self, report_cog: KvkReport, conn, event_id: int, free_mode: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.report_cog = report_cog
        self.conn = conn
        self.event_id = event_id
        self.free_mode = free_mode
        self.names = _alliance_names()
        self.selected_type: str | None = None
        self.selected_alliance: int | None = None
        self.selected_index: int | None = None
        self.player_page = 0
        self.nicknames: dict = {}
        self.groups: list = []
        self._build_items()

    def _slot_mode(self) -> int:
        ev = kvkdb.get_event(self.conn, self.event_id)
        return ev["slot_mode"] if ev else 0

    def _group_rows(self) -> list:
        """Occupied rows of the selected group, ordered by real time (slot_time)."""
        if self.selected_type is None:
            return []
        grid_pos = {t: i for i, t in enumerate(generate_time_slots(self._slot_mode()))}
        rows = [r for r in kvkdb.get_slots(self.conn, self.event_id)
                if r["position_type"] == self.selected_type
                and r["alliance_id"] == self.selected_alliance and r["fid"] is not None]
        rows.sort(key=lambda r: grid_pos.get(r["slot_time"], 0))
        return rows

    def _build_items(self) -> None:
        """(Re)build the components. The group select is always shown; the player select and the
        per-player actions appear once a group is picked. A Discord select caps at 25 options, so
        the player list paginates with Previous / Next."""
        self.clear_items()
        self.groups = _distinct_groups(self.conn, self.event_id)
        if (self.selected_type, self.selected_alliance) not in self.groups:
            self.selected_type = None
            self.selected_alliance = None
            self.selected_index = None
            self.player_page = 0

        if self.groups:
            self.add_item(_GroupSelect(self, self.groups[:_MAX_GROUP_OPTIONS]))

        if self.selected_type is not None:
            rows = self._group_rows()
            self.nicknames = _nicknames_for({r["fid"] for r in rows})
            if self.selected_index not in {r["slot_index"] for r in rows}:
                self.selected_index = None
            total_pages = max(1, (len(rows) + self.PLAYER_PAGE_SIZE - 1) // self.PLAYER_PAGE_SIZE)
            self.player_page = min(self.player_page, total_pages - 1)
            start = self.player_page * self.PLAYER_PAGE_SIZE
            page_rows = rows[start:start + self.PLAYER_PAGE_SIZE]
            if page_rows:
                self.add_item(_PlayerSelect(self, page_rows, self.player_page, total_pages))

            for label, cb, style in (
                ("Lock", self.lock, discord.ButtonStyle.secondary),
                ("Unlock", self.unlock, discord.ButtonStyle.secondary),
                ("Move...", self.move_prompt, discord.ButtonStyle.secondary),
                ("Remove", self.remove, discord.ButtonStyle.danger),
            ):
                button = discord.ui.Button(label=label, style=style, row=2)
                button.callback = cb
                self.add_item(button)

            if total_pages > 1:
                prev_button = discord.ui.Button(
                    label="Previous", emoji=theme.prevIcon, style=discord.ButtonStyle.secondary,
                    row=3, disabled=self.player_page == 0)
                prev_button.callback = self.prev_page
                self.add_item(prev_button)
                next_button = discord.ui.Button(
                    label="Next", emoji=theme.nextIcon, style=discord.ButtonStyle.secondary,
                    row=3, disabled=self.player_page >= total_pages - 1)
                next_button.callback = self.next_page
                self.add_item(next_button)

        if self.groups:
            rerun_button = discord.ui.Button(label="Re-run", style=discord.ButtonStyle.primary, row=4)
            rerun_button.callback = self.rerun
            self.add_item(rerun_button)

    @property
    def truncated(self) -> bool:
        return len(self.groups) > _MAX_GROUP_OPTIONS

    def _find_row(self, index):
        for row in kvkdb.get_slots(self.conn, self.event_id):
            if (row["position_type"] == self.selected_type
                    and row["alliance_id"] == self.selected_alliance
                    and row["slot_index"] == index):
                return row
        return None

    async def _selected_row(self, interaction: discord.Interaction):
        if self.selected_type is None:
            await interaction.response.send_message("Pick a group first.", ephemeral=True)
            return None
        if self.selected_index is None:
            await interaction.response.send_message("Pick a player first.", ephemeral=True)
            return None
        row = self._find_row(self.selected_index)
        if row is None or row["fid"] is None:
            await interaction.response.send_message("That player is no longer in this group.", ephemeral=True)
            return None
        return row

    async def _refresh(self, interaction: discord.Interaction):
        self._build_items()
        embeds = self.report_cog._render_embeds(self.conn, self.event_id)
        await interaction.response.edit_message(embeds=embeds[:_MAX_EMBEDS_PER_MESSAGE], view=self)
        for extra in range(_MAX_EMBEDS_PER_MESSAGE, len(embeds), _MAX_EMBEDS_PER_MESSAGE):
            await interaction.followup.send(embeds=embeds[extra:extra + _MAX_EMBEDS_PER_MESSAGE], ephemeral=True)
        if not self.groups:
            await interaction.followup.send("No groups to edit yet (no signups assigned).", ephemeral=True)

    async def lock(self, interaction: discord.Interaction):
        row = await self._selected_row(interaction)
        if row is None:
            return
        kvkdb.set_slot(self.conn, self.event_id, self.selected_type, self.selected_alliance,
                       self.selected_index, row["fid"], 1)
        await self._refresh(interaction)

    async def unlock(self, interaction: discord.Interaction):
        row = await self._selected_row(interaction)
        if row is None:
            return
        kvkdb.set_slot(self.conn, self.event_id, self.selected_type, self.selected_alliance,
                       self.selected_index, row["fid"], 0)
        await self._refresh(interaction)

    async def remove(self, interaction: discord.Interaction):
        row = await self._selected_row(interaction)
        if row is None:
            return
        kvkdb.set_slot(self.conn, self.event_id, self.selected_type, self.selected_alliance,
                       self.selected_index, None, 0)
        self.selected_index = None
        await self._refresh(interaction)

    async def move_prompt(self, interaction: discord.Interaction):
        if await self._selected_row(interaction) is not None:
            await interaction.response.send_modal(_MoveTimeModal(self))

    async def do_move(self, interaction: discord.Interaction, time_text: str):
        row = self._find_row(self.selected_index) if self.selected_index is not None else None
        if row is None or row["fid"] is None:
            await interaction.response.send_message("Pick a player first.", ephemeral=True)
            return
        slot_mode = self._slot_mode()
        grid = generate_time_slots(slot_mode)
        try:
            picks = parse_desired_slots(time_text, slot_mode)
        except ValueError:
            picks = []
        if len(picks) != 1:
            await interaction.response.send_message("Enter a single time like 15:00.", ephemeral=True)
            return
        gi = picks[0]
        new_time = grid[gi]
        if self.free_mode:
            # Kingdom grid: the target time is a real slot; swap the two slots' occupants.
            target = self._find_row(gi)
            if target is None:
                await interaction.response.send_message("That time is not on the grid.", ephemeral=True)
                return
            kvkdb.set_slot(self.conn, self.event_id, self.selected_type, self.selected_alliance,
                           self.selected_index, target["fid"], target["locked"])
            kvkdb.set_slot(self.conn, self.event_id, self.selected_type, self.selected_alliance,
                           gi, row["fid"], row["locked"])
            self.selected_index = gi
            await self._refresh(interaction)
            return
        # Alliance: slot_index is opaque, so rewrite the time. If a same-role player already holds that
        # time, swap their times so no two of the same role collide.
        role = row.get("role", "")
        conflict = next(
            (r for r in self._group_rows()
             if r.get("role", "") == role and r["slot_time"] == new_time
             and r["slot_index"] != self.selected_index), None)
        if conflict:
            kvkdb.set_slot_time(self.conn, self.event_id, self.selected_type, self.selected_alliance,
                                conflict["slot_index"], row["slot_time"])
        kvkdb.set_slot_time(self.conn, self.event_id, self.selected_type, self.selected_alliance,
                            self.selected_index, new_time)
        await self._refresh(interaction)

    async def prev_page(self, interaction: discord.Interaction):
        self.player_page = max(0, self.player_page - 1)
        await self._refresh(interaction)

    async def next_page(self, interaction: discord.Interaction):
        self.player_page += 1  # clamped in _build_items
        await self._refresh(interaction)

    async def rerun(self, interaction: discord.Interaction):
        self.report_cog._compute(self.conn, self.event_id)
        await self._refresh(interaction)


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


async def setup(bot):
    await bot.add_cog(KvkReport(bot))

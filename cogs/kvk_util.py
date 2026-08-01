"""Pure, dependency-free helpers for the KvK scheduler (no discord/sqlite imports)."""
import re
from datetime import datetime, timedelta

POSITION_TYPES = ("Training", "Research", "Building")

# KvK day of each minister position, as an offset in days from the event start date (day 1).
_TYPE_DAY_OFFSET = {"Building": 0, "Research": 1, "Training": 3}


def type_dates_for(event_date: str, active_types) -> list[tuple[str, str]]:
    """Compute each active position type's date from the event start date (day 1).

    Building = day 1 (offset 0), Research = day 2 (+1), Training = day 4 (+3).
    Returns (position_type, YYYY-MM-DD) pairs in day order.
    """
    base = datetime.strptime(event_date, "%Y-%m-%d")
    ordered = sorted(active_types, key=lambda t: _TYPE_DAY_OFFSET[t])
    return [(t, (base + timedelta(days=_TYPE_DAY_OFFSET[t])).strftime("%Y-%m-%d")) for t in ordered]

_UNIT_MINUTES = {"d": 1440, "h": 60, "m": 1}
_TOKEN_RE = re.compile(r"(\d+)\s*([dhm])")


def parse_speedups(text: str) -> int:
    """Parse a free-form speedup duration ('7d 12h', '70h', '2d4h30m') into total minutes.

    Units: d=1440m, h=60m, m=1m. Spaces optional, case-insensitive.
    Raises ValueError on empty input, no unit token, or stray characters.
    """
    if not text or not text.strip():
        raise ValueError("empty speedup value")
    cleaned = text.strip().lower()
    total = 0
    matched = False
    for amount, unit in _TOKEN_RE.findall(cleaned):
        total += int(amount) * _UNIT_MINUTES[unit]
        matched = True
    if not matched or _TOKEN_RE.sub("", cleaned).strip():
        raise ValueError(f"could not parse speedups: {text!r}")
    return total


def format_speedups(minutes: int) -> str:
    """Format total minutes back into 'Nd Nh Nm', dropping zero parts. 0 -> '0m'. Inverse of parse."""
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    return " ".join(parts) if parts else "0m"


def generate_time_slots(slot_mode: int) -> list[str]:
    """Daily 30-min slot grid. Mode 0: 00:00..23:30 (48). Mode 1: offset, 49 entries.
    Mirrors MinisterSchedule.get_time_slots (cogs/minister_schedule.py:288)."""
    slots: list[str] = []
    if slot_mode == 0:
        for hour in range(24):
            for minute in (0, 30):
                slots.append(f"{hour:02}:{minute:02}")
    else:
        slots.append("00:00")
        for hour in range(24):
            for minute in (15, 45):
                if hour == 23 and minute == 45:
                    slots.append("23:45")
                    break
                slots.append(f"{hour:02}:{minute:02}")
    return slots


_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _time_to_minutes(token: str):
    """'HH:MM' -> minutes of day, or None if malformed or out of range."""
    m = _TIME_RE.fullmatch(token.strip())
    if not m:
        return None
    hours, mins = int(m.group(1)), int(m.group(2))
    if hours > 23 or mins > 59:
        return None
    return hours * 60 + mins


def parse_desired_slots(text: str, slot_mode: int) -> list[int]:
    """Parse preferred times into grid slot indices for the given slot mode.

    Comma-separated tokens; each is 'HH:MM' or a 'HH:MM-HH:MM' range. A range selects every grid
    slot whose time is within [start, end]; a lone time snaps to the slot covering it (the latest
    grid slot at or before it). Empty input -> []. Raises ValueError on a malformed token.
    """
    if not text or not text.strip():
        return []
    grid = [_time_to_minutes(t) for t in generate_time_slots(slot_mode)]
    wanted: set = set()
    for raw in text.split(","):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            start, end = _time_to_minutes(start_s), _time_to_minutes(end_s)
            if start is None or end is None or start > end:
                raise ValueError(f"bad time range: {token!r}")
            wanted.update(i for i, m in enumerate(grid) if start <= m <= end)
        else:
            point = _time_to_minutes(token)
            if point is None:
                raise ValueError(f"bad time: {token!r}")
            covering = [i for i, m in enumerate(grid) if m <= point]
            wanted.add(covering[-1] if covering else 0)
    return sorted(wanted)


def rank_and_assign(signups, slot_count, slot_mode, locked=None):
    """Rank signups by speedups, honor each player's desired slots, then fill the rest by rank.

    Each signup may carry "desired_slots" (a list of slot indices). In rank order a player is placed
    in their first free desired slot; players with no free desired slot fill the remaining slots by
    index order. Locked slots (locked={index: fid}) are pre-placed and never reassigned. With no
    desired slots this reduces to the old behavior: rank order fills slots by index.
    """
    locked = dict(locked or {})
    times = generate_time_slots(slot_mode)
    ranked = sorted(signups, key=lambda s: (-s["speedup_minutes"], s["submitted_at"], s["fid"]))

    assigned = dict(locked)              # slot_index -> fid
    taken_fids = set(locked.values())
    leftover = []                        # ranked fids not placed by preference
    for s in ranked:
        fid = s["fid"]
        if fid in taken_fids:
            continue                     # already holds a locked slot
        placed = False
        for i in s.get("desired_slots", []):
            if 0 <= i < slot_count and i not in assigned:
                assigned[i] = fid
                taken_fids.add(fid)
                placed = True
                break
        if not placed:
            leftover.append(fid)

    free = (i for i in range(slot_count) if i not in assigned)
    for fid in leftover:
        i = next(free, None)
        if i is None:
            break                        # more players than slots
        assigned[i] = fid

    return [
        {"slot_index": i, "slot_time": times[i] if i < len(times) else "",
         "fid": assigned.get(i), "locked": 1 if i in locked else 0}
        for i in range(slot_count)
    ]

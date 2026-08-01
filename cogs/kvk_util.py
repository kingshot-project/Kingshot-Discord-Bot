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


def rank_and_assign(signups, slot_count, slot_mode, locked=None):
    """Rank signups by speedups and place them into slot_count slots (see module docstring)."""
    locked = dict(locked or {})
    times = generate_time_slots(slot_mode)
    ranked = sorted(signups, key=lambda s: (-s["speedup_minutes"], s["submitted_at"], s["fid"]))
    locked_fids = set(locked.values())
    pool = [s["fid"] for s in ranked if s["fid"] not in locked_fids]
    result = []
    pi = 0
    for i in range(slot_count):
        slot_time = times[i] if i < len(times) else ""
        if i in locked:
            result.append({"slot_index": i, "slot_time": slot_time, "fid": locked[i], "locked": 1})
        elif pi < len(pool):
            result.append({"slot_index": i, "slot_time": slot_time, "fid": pool[pi], "locked": 0})
            pi += 1
        else:
            result.append({"slot_index": i, "slot_time": slot_time, "fid": None, "locked": 0})
    return result

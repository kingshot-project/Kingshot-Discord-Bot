"""Pure, dependency-free helpers for the KvK scheduler (no discord/sqlite imports)."""
import math
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


# KvK "Pro mode" training-day scoring. Time in seconds, KvK points per troop, by troop tier.
KVK_TRAIN_TIME = {1: 12, 2: 17, 3: 24, 4: 32, 5: 44, 6: 60, 7: 83, 8: 113, 9: 131, 10: 152, 11: 180}
KVK_POINTS = {1: 3, 2: 4, 3: 5, 4: 8, 5: 12, 6: 18, 7: 25, 8: 35, 9: 45, 10: 60, 11: 75}
MAX_TROOP_LEVEL = 11

_COUNT_SUFFIX = {"k": 1000, "к": 1000, "m": 1_000_000, "м": 1_000_000}  # Latin + Cyrillic k/m


def parse_troop_count(text: str) -> int:
    """Parse a troop count like '900k', '900000', '1.5m' (Latin or Cyrillic k/m) into an int. '' -> 0."""
    s = text.strip().lower().replace(",", "").replace(" ", "")
    if not s:
        return 0
    mult = 1
    if s[-1] in _COUNT_SUFFIX:
        mult = _COUNT_SUFFIX[s[-1]]
        s = s[:-1]
    try:
        value = float(s) * mult
    except ValueError:
        raise ValueError(f"could not parse troop count: {text!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"troop count is not a finite number: {text!r}")
    if value < 0:
        raise ValueError(f"troop count cannot be negative: {text!r}")
    return int(value)


def compute_training_points(base_level, hours_minutes, upgrade_from=None, upgrade_count=0) -> dict:
    """KvK training-day points from speedup time, honoring optional troop upgrades first.

    Upgrades (upgrade_count troops from upgrade_from up to base_level) each take
    training_time[base] - training_time[from] seconds and earn points[base] - points[from]. Only as
    many as the speedup time allows are upgraded; the rest of the time trains new base-level troops
    (points[base] each). Returns a breakdown dict; the total is under "kvk_points".
    """
    if base_level not in KVK_POINTS:
        raise ValueError(f"unknown troop level: {base_level}")
    total_seconds = max(0, int(hours_minutes)) * 60
    base_time = KVK_TRAIN_TIME[base_level]
    base_points = KVK_POINTS[base_level]

    upgraded = 0
    upgrade_points = 0
    remaining = total_seconds
    if upgrade_count and upgrade_count > 0 and upgrade_from is not None:
        if upgrade_from not in KVK_POINTS:
            raise ValueError(f"unknown upgrade level: {upgrade_from}")
        if upgrade_from >= base_level:
            raise ValueError("upgrade level must be below the base troop level")
        time_each = base_time - KVK_TRAIN_TIME[upgrade_from]  # positive: times increase with tier
        upgraded = min(upgrade_count, remaining // time_each)
        remaining -= upgraded * time_each
        upgrade_points = upgraded * (base_points - KVK_POINTS[upgrade_from])

    new_troops = remaining // base_time
    new_points = new_troops * base_points
    return {
        "kvk_points": upgrade_points + new_points,
        "new_troops": new_troops, "new_points": new_points,
        "upgraded": upgraded, "upgrade_points": upgrade_points,
    }


# Base-level choices for Pro mode. TG* are subgroups that share their tier's time/points; they can be
# picked as a base level, but troops are never upgraded from or to a TG (only between plain tiers).
TROOP_LEVELS = [
    "T11", "T11-TG5", "T10", "T10-TG1", "T10-TG2", "T10-TG3", "T10-TG4", "T10-TG5",
    "T9", "T8", "T7", "T6", "T5", "T4", "T3", "T2", "T1",
]
_TG_TIER = {"T11-TG5": 11, "T10-TG1": 10, "T10-TG2": 10, "T10-TG3": 10, "T10-TG4": 10, "T10-TG5": 10}


def troop_tier(label) -> int:
    """Resolve a base-level label ('T10', 'T10-TG3', 'T11-TG5', or '10') to its scoring tier (1-11)."""
    text = str(label).strip()
    if text in _TG_TIER:
        return _TG_TIER[text]
    m = re.fullmatch(r"[Tt]?(\d+)", text)
    if m and int(m.group(1)) in KVK_POINTS:
        return int(m.group(1))
    raise ValueError(f"unknown troop level: {label!r}")


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
    # Rank by "score" when the caller sets it (Pro-mode training uses KvK points); else by speedups.
    ranked = sorted(
        signups, key=lambda s: (-s.get("score", s["speedup_minutes"]), s["submitted_at"], s["fid"]))

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

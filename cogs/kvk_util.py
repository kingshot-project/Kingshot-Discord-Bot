"""Pure, dependency-free helpers for the KvK scheduler (no discord/sqlite imports)."""
import re

POSITION_TYPES = ("Training", "Research", "Building")

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

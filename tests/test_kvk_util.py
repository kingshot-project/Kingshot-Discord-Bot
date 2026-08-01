import pytest

from cogs.kvk_util import generate_time_slots, parse_speedups


@pytest.mark.parametrize("text,minutes", [
    ("70h", 70 * 60), ("7d", 7 * 1440), ("7d 12h", 7 * 1440 + 12 * 60),
    ("2d4h30m", 2 * 1440 + 4 * 60 + 30), ("90m", 90), ("  3D ", 3 * 1440),
    ("1d 1h 1m", 1440 + 60 + 1),
])
def test_parse_speedups_ok(text, minutes):
    assert parse_speedups(text) == minutes


@pytest.mark.parametrize("bad", ["", "abc", "12", "5x", "d", "h m"])
def test_parse_speedups_invalid(bad):
    with pytest.raises(ValueError):
        parse_speedups(bad)


def test_slots_mode0():
    slots = generate_time_slots(0)
    assert len(slots) == 48 and slots[0] == "00:00" and slots[1] == "00:30" and slots[-1] == "23:30"


def test_slots_mode1():
    slots = generate_time_slots(1)
    assert len(slots) == 49 and slots[0] == "00:00" and slots[1] == "00:15" and slots[-1] == "23:45"

import pytest

from cogs.kvk_util import generate_time_slots, parse_speedups, rank_and_assign


@pytest.mark.parametrize("text,minutes", [
    ("70h", 70 * 60), ("7d", 7 * 1440), ("7d 12h", 7 * 1440 + 12 * 60),
    ("2d4h30m", 2 * 1440 + 4 * 60 + 30), ("90m", 90), ("  3D ", 3 * 1440),
    ("1d 1h 1m", 1440 + 60 + 1),
])
def test_parse_speedups_ok(text, minutes):
    assert parse_speedups(text) == minutes


@pytest.mark.parametrize("bad", ["", "abc", "12", "5x", "d", "h m", "5d3x"])
def test_parse_speedups_invalid(bad):
    with pytest.raises(ValueError):
        parse_speedups(bad)


def test_slots_mode0():
    slots = generate_time_slots(0)
    assert len(slots) == 48 and slots[0] == "00:00" and slots[1] == "00:30" and slots[-1] == "23:30"


def test_slots_mode1():
    slots = generate_time_slots(1)
    assert len(slots) == 49 and slots[0] == "00:00" and slots[1] == "00:15" and slots[-1] == "23:45"


def _s(fid, minutes, at):
    return {"fid": fid, "speedup_minutes": minutes, "submitted_at": at}


def test_assign_orders_by_speedups_desc():
    out = rank_and_assign([_s(1, 100, "t1"), _s(2, 300, "t2"), _s(3, 200, "t3")], 3, 0)
    assert [r["fid"] for r in out] == [2, 3, 1]
    assert out[0]["slot_time"] == "00:00" and out[1]["slot_time"] == "00:30"


def test_assign_tiebreak_earlier_then_lower_fid():
    out = rank_and_assign([_s(5, 100, "t2"), _s(2, 100, "t2"), _s(9, 100, "t1")], 3, 0)
    assert [r["fid"] for r in out] == [9, 2, 5]


def test_assign_caps_and_leaves_empty_slots():
    out = rank_and_assign([_s(1, 100, "t1")], 3, 0)
    assert [r["fid"] for r in out] == [1, None, None]


def test_assign_respects_locks():
    out = rank_and_assign([_s(1, 500, "t1"), _s(2, 400, "t2"), _s(3, 300, "t3")], 3, 0, locked={0: 3})
    assert out[0]["fid"] == 3 and out[0]["locked"] == 1
    assert [r["fid"] for r in out[1:]] == [1, 2]


def test_assign_kingdom_49():
    out = rank_and_assign([_s(i, 1000 - i, f"t{i:03}") for i in range(60)], 49, 1)
    assert len(out) == 49 and out[0]["fid"] == 0 and out[-1]["slot_time"] == "23:45"

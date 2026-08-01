import pytest

from cogs.kvk_util import (
    format_speedups,
    generate_time_slots,
    parse_desired_slots,
    parse_speedups,
    rank_and_assign,
    type_dates_for,
)


@pytest.mark.parametrize("text,indices", [
    ("", []),
    ("20:00", [40]),                       # mode 0: index i -> i*30 min; 20:00 = 40
    ("20:00-21:00", [40, 41, 42]),         # 20:00, 20:30, 21:00
    ("20:00, 22:00", [40, 44]),
    ("20:15", [40]),                       # off-grid single time snaps to the covering slot 20:00
    ("20:00-21:00, 23:30", [40, 41, 42, 47]),
    ("00:00", [0]),
])
def test_parse_desired_slots_ok(text, indices):
    assert parse_desired_slots(text, 0) == indices


@pytest.mark.parametrize("bad", ["abc", "25:00", "20:00-19:00", "20:70", "20"])
def test_parse_desired_slots_bad(bad):
    with pytest.raises(ValueError):
        parse_desired_slots(bad, 0)


def test_rank_and_assign_honors_desired():
    signups = [
        {"fid": 1, "speedup_minutes": 100, "submitted_at": "t", "desired_slots": [2]},
        {"fid": 2, "speedup_minutes": 50, "submitted_at": "t", "desired_slots": [2]},
    ]
    by_slot = {r["slot_index"]: r["fid"] for r in rank_and_assign(signups, 4, 0)}
    assert by_slot[2] == 1                  # higher speedup wins the contested slot
    assert by_slot[0] == 2                  # loser falls back to the first free slot
    assert by_slot[1] is None and by_slot[3] is None


def test_rank_and_assign_no_desired_is_sequential():
    signups = [
        {"fid": 1, "speedup_minutes": 100, "submitted_at": "t", "desired_slots": []},
        {"fid": 2, "speedup_minutes": 50, "submitted_at": "t"},   # missing key = no preference
    ]
    assert [r["fid"] for r in rank_and_assign(signups, 3, 0)] == [1, 2, None]


def test_rank_and_assign_desired_out_of_range_falls_back():
    signups = [{"fid": 1, "speedup_minutes": 100, "submitted_at": "t", "desired_slots": [99]}]
    assert rank_and_assign(signups, 3, 0)[0]["fid"] == 1   # index 99 >= 3 ignored -> slot 0


def test_rank_and_assign_locked_slot_blocks_desired():
    signups = [{"fid": 1, "speedup_minutes": 100, "submitted_at": "t", "desired_slots": [1]}]
    by_slot = {r["slot_index"]: (r["fid"], r["locked"]) for r in rank_and_assign(signups, 3, 0, locked={1: 9})}
    assert by_slot[1] == (9, 1)             # locked slot stays
    assert by_slot[0] == (1, 0)             # desired slot taken -> fallback to slot 0


@pytest.mark.parametrize("minutes,text", [
    (7200, "5d"), (300, "5h"), (600, "10h"), (0, "0m"), (1440, "1d"),
    (1500, "1d 1h"), (1501, "1d 1h 1m"), (90, "1h 30m"), (4200, "2d 22h"),
])
def test_format_speedups(minutes, text):
    assert format_speedups(minutes) == text


def test_type_dates_for_offsets_and_day_order():
    # Building = day 1 (offset 0), Research = day 2 (+1), Training = day 4 (+3), returned in day order.
    got = type_dates_for("2026-09-01", ["Training", "Research", "Building"])
    assert got == [("Building", "2026-09-01"), ("Research", "2026-09-02"), ("Training", "2026-09-04")]


def test_type_dates_for_subset_and_month_rollover():
    assert type_dates_for("2026-09-30", ["Training"]) == [("Training", "2026-10-03")]
    assert type_dates_for("2026-09-01", ["Research"]) == [("Research", "2026-09-02")]


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

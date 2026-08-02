import pytest

from cogs.kvk_util import (
    SHARED_TRAINING_BONUS,
    compute_training_points,
    format_speedups,
    generate_time_slots,
    parse_desired_slots,
    parse_percent,
    parse_speedups,
    parse_troop_count,
    rank_and_assign,
    troop_tier,
    type_dates_for,
)


def test_compute_training_points_example():
    # base T10, 20h, upgrade 900k from T9: 3428 fit (21s each), 51420 pts, ~0 new
    r = compute_training_points(10, 20 * 60, upgrade_from=9, upgrade_count=900_000)
    assert r["upgraded"] == 3428
    assert r["upgrade_points"] == 3428 * 15
    assert r["new_troops"] == 0
    assert r["kvk_points"] == 51420


def test_compute_training_points_no_upgrade():
    r = compute_training_points(10, 20 * 60)   # 72000s / 152s = 473 troops
    assert r["new_troops"] == 473 and r["kvk_points"] == 473 * 60
    assert r["upgraded"] == 0


def test_compute_training_points_upgrade_fits():
    r = compute_training_points(10, 10, upgrade_from=9, upgrade_count=5)  # 600s
    assert r["upgraded"] == 5 and r["upgrade_points"] == 5 * 15
    assert r["new_troops"] == 3 and r["kvk_points"] == 5 * 15 + 3 * 60  # 255


@pytest.mark.parametrize("kwargs", [
    {"base_level": 12, "hours_minutes": 60},                              # unknown base level
    {"base_level": 10, "hours_minutes": 60, "upgrade_from": 10, "upgrade_count": 5},  # from >= base
    {"base_level": 10, "hours_minutes": 60, "upgrade_from": 12, "upgrade_count": 5},  # unknown from
])
def test_compute_training_points_bad(kwargs):
    with pytest.raises(ValueError):
        compute_training_points(**kwargs)


def test_compute_training_points_with_speed_time_capped():
    # base T10, 80h (4800 min), upgrade from T9, huge stock, training speed +202.9%.
    # Training speed stretches the 288000 s budget to 288000*3.029 = 872352 s. Per-upgrade cost stays
    # the raw 21 s (the game only floors the running total: 1/2/3/4 troops show 6/13/20/27 s), so the
    # count is floor(872352 / 21) = 41540 - NOT floor(288000 / 6) = 48000 from flooring each troop.
    r = compute_training_points(10, 80 * 60, upgrade_from=9, upgrade_count=900_000, training_speed=202.9)
    assert r["upgraded"] == 41540
    assert r["kvk_points"] == 41540 * 15 == 623_100
    assert r["new_troops"] == 0


def test_compute_training_points_with_speed_count_capped():
    # Same but only 13714 T9 in stock: upgrade them all, spend the rest of the stretched budget on
    # new T10 troops (raw 152 s each).
    r = compute_training_points(10, 80 * 60, upgrade_from=9, upgrade_count=13_714, training_speed=202.9)
    assert r["upgraded"] == 13_714 and r["upgrade_points"] == 205_710
    assert r["new_troops"] == 3_844 and r["kvk_points"] == 205_710 + 3_844 * 60  # 436350


def test_shared_training_bonus_is_kingdom_kvk_position():
    assert SHARED_TRAINING_BONUS == 105.0  # 30 kingdom + 25 KvK + 50 position


def test_compute_training_points_full_stack():
    # own 202.9% + shared 105% = 307.9% total: 80h stretches to 288000*4.079 = 1174752 s,
    # floor(1174752 / 21) = 55940 upgrades = 839100 pts.
    total = 202.9 + SHARED_TRAINING_BONUS
    r = compute_training_points(10, 80 * 60, upgrade_from=9, upgrade_count=900_000, training_speed=total)
    assert r["upgraded"] == 55_940 and r["kvk_points"] == 839_100
    # Breakdown values the receipt shows; the derivation must reconcile: floor(effective/each) == count.
    assert r["total_seconds"] == 288_000 and r["effective_seconds"] == 1_174_752
    assert r["upgrade_time_each"] == 21 and r["upgrade_point_each"] == 15
    assert r["new_time_each"] == 152 and r["new_point_each"] == 60
    assert r["effective_seconds"] // r["upgrade_time_each"] == r["upgraded"]


def test_compute_training_points_speed_zero_matches_no_speed():
    a = compute_training_points(10, 20 * 60, upgrade_from=9, upgrade_count=900_000)
    b = compute_training_points(10, 20 * 60, upgrade_from=9, upgrade_count=900_000, training_speed=0)
    assert a == b


@pytest.mark.parametrize("text,value", [
    ("202.9", 202.9), ("+202.9%", 202.9), ("202,9", 202.9), ("0", 0.0),
    ("100", 100.0), ("  50% ", 50.0),
])
def test_parse_percent_ok(text, value):
    assert parse_percent(text) == value


@pytest.mark.parametrize("bad", ["", "abc", "-5", "inf", "nan", "1e999", "%"])
def test_parse_percent_bad(bad):
    with pytest.raises(ValueError):
        parse_percent(bad)


@pytest.mark.parametrize("text,count", [
    ("900k", 900_000), ("900к", 900_000), ("1.5m", 1_500_000), ("900000", 900_000),
    ("900,000", 900_000), ("", 0), ("2м", 2_000_000),
])
def test_parse_troop_count_ok(text, count):
    assert parse_troop_count(text) == count


@pytest.mark.parametrize("bad", ["abc", "-5", "1..5k", "inf", "1e999", "nan"])
def test_parse_troop_count_bad(bad):
    with pytest.raises(ValueError):
        parse_troop_count(bad)


@pytest.mark.parametrize("label,tier", [
    ("T10", 10), ("T10-TG3", 10), ("T11-TG5", 11), ("10", 10), ("T11", 11), ("t9", 9),
])
def test_troop_tier_ok(label, tier):
    assert troop_tier(label) == tier


@pytest.mark.parametrize("bad", ["T12", "TGx", "abc", "T0"])
def test_troop_tier_bad(bad):
    with pytest.raises(ValueError):
        troop_tier(bad)


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

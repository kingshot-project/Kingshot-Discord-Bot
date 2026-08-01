import importlib
import sqlite3

kvk = importlib.import_module("cogs.kvk_data")


def _conn():
    c = sqlite3.connect(":memory:")
    kvk.init_schema(c)
    return c


def test_create_event_and_active_types():
    c = _conn()
    eid = kvk.create_event(
        c, guild_id=1, name="KvK 42", event_date="2026-09-01", scope="alliance",
        slots_per_alliance=5, slot_mode=0, signup_open_at="2026-08-20 00:00",
        signup_close_at="2026-08-31 00:00", publish_channel_id=99, created_by=7,
        created_at="2026-08-19 00:00",
    )
    kvk.set_event_types(c, eid, [("Training", "2026-09-01"), ("Research", "2026-09-02")])
    ev = kvk.get_event(c, eid)
    assert ev["name"] == "KvK 42" and ev["status"] == "collecting"
    assert set(kvk.get_active_types(c, eid)) == {"Training", "Research"}


def test_signup_upsert_idempotent():
    c = _conn()
    eid = kvk.create_event(
        c, guild_id=1, name="K", event_date="2026-09-01", scope="kingdom",
        slots_per_alliance=None, slot_mode=1, signup_open_at="a", signup_close_at="b",
        publish_channel_id=None, created_by=1, created_at="c",
    )
    kvk.upsert_signup(c, eid, 100, "Training", 600, 100, "t1")
    kvk.upsert_signup(c, eid, 100, "Training", 900, 100, "t2")
    assert kvk.get_signups(c, eid, "Training") == [{"fid": 100, "speedup_minutes": 900, "submitted_at": "t2"}]


def test_list_events_scoped_to_guild_newest_first():
    c = _conn()
    e1 = kvk.create_event(
        c, guild_id=10, name="First", event_date="2026-09-01", scope="kingdom",
        slots_per_alliance=None, slot_mode=0, signup_open_at="a", signup_close_at="b",
        publish_channel_id=None, created_by=1, created_at="c",
    )
    e2 = kvk.create_event(
        c, guild_id=10, name="Second", event_date="2026-09-02", scope="alliance",
        slots_per_alliance=3, slot_mode=0, signup_open_at="a", signup_close_at="b",
        publish_channel_id=None, created_by=1, created_at="c",
    )
    kvk.create_event(
        c, guild_id=99, name="Other guild", event_date="2026-09-03", scope="kingdom",
        slots_per_alliance=None, slot_mode=0, signup_open_at="a", signup_close_at="b",
        publish_channel_id=None, created_by=1, created_at="c",
    )
    events = kvk.list_events(c, 10)
    assert [e["id"] for e in events] == [e2, e1]  # newest first, other guild excluded
    assert events[0]["name"] == "Second" and events[0]["scope"] == "alliance"


def test_list_open_events_filters_status_window_and_guild():
    c = _conn()
    common = {
        "event_date": "2026-09-01", "scope": "kingdom", "slots_per_alliance": None, "slot_mode": 0,
        "publish_channel_id": None, "created_by": 1, "created_at": "x",
    }
    open_now = kvk.create_event(
        c, guild_id=5, name="Open", signup_open_at="2026-08-01 00:00",
        signup_close_at="2026-08-31 00:00", **common)
    kvk.create_event(
        c, guild_id=5, name="Not yet", signup_open_at="2026-09-01 00:00",
        signup_close_at="2026-09-30 00:00", **common)  # window not started
    closed = kvk.create_event(
        c, guild_id=5, name="Closed", signup_open_at="2026-07-01 00:00",
        signup_close_at="2026-07-31 00:00", **common)  # window ended
    kvk.set_status(c, closed, "assigned")  # also not 'collecting'
    kvk.create_event(
        c, guild_id=99, name="Other guild", signup_open_at="2026-08-01 00:00",
        signup_close_at="2026-08-31 00:00", **common)  # different guild

    events = kvk.list_open_events(c, 5, "2026-08-15 12:00")
    assert [e["id"] for e in events] == [open_now]
    assert events[0]["name"] == "Open" and events[0]["event_date"] == "2026-09-01"


def test_delete_event_removes_all_related_rows():
    c = _conn()
    keep = kvk.create_event(
        c, guild_id=1, name="Keep", event_date="d", scope="kingdom", slots_per_alliance=None,
        slot_mode=0, signup_open_at="a", signup_close_at="b", publish_channel_id=None,
        created_by=1, created_at="c")
    kvk.set_event_types(c, keep, [("Training", "d")])
    kvk.upsert_signup(c, keep, 1, "Training", 10, 1, "t")

    doomed = kvk.create_event(
        c, guild_id=1, name="Doomed", event_date="d", scope="kingdom", slots_per_alliance=None,
        slot_mode=0, signup_open_at="a", signup_close_at="b", publish_channel_id=None,
        created_by=1, created_at="c")
    kvk.set_event_types(c, doomed, [("Training", "d"), ("Research", "d")])
    kvk.upsert_signup(c, doomed, 99, "Training", 500, 99, "t")
    kvk.save_slots(c, doomed, "Training", 0, [{"slot_index": 0, "slot_time": "00:00", "fid": 99, "locked": 0}])

    kvk.delete_event(c, doomed)

    assert kvk.get_event(c, doomed) is None
    for table in ("kvk_event_types", "kvk_signups", "kvk_slots"):
        n = c.execute(f"SELECT COUNT(*) FROM {table} WHERE event_id = ?", (doomed,)).fetchone()[0]
        assert n == 0, f"{table} still has rows for the deleted event"
    # the other event is untouched
    assert kvk.get_event(c, keep)["name"] == "Keep"
    assert kvk.get_active_types(c, keep) == ["Training"]
    assert kvk.get_signups(c, keep, "Training") == [{"fid": 1, "speedup_minutes": 10, "submitted_at": "t"}]


def test_slots_save_override_and_locks():
    c = _conn()
    eid = kvk.create_event(
        c, guild_id=1, name="K", event_date="d", scope="alliance",
        slots_per_alliance=2, slot_mode=0, signup_open_at="a", signup_close_at="b",
        publish_channel_id=None, created_by=1, created_at="c",
    )
    kvk.save_slots(c, eid, "Training", 5, [
        {"slot_index": 0, "slot_time": "00:00", "fid": 1, "locked": 0},
        {"slot_index": 1, "slot_time": "00:30", "fid": None, "locked": 0},
    ])
    kvk.set_slot(c, eid, "Training", 5, 1, fid=42, locked=1)
    got = {(r["slot_index"], r["fid"], r["locked"]) for r in kvk.get_slots(c, eid)}
    assert (0, 1, 0) in got and (1, 42, 1) in got
    assert kvk.get_locks(c, eid, "Training", 5) == {1: 42}
    # Test override: second save_slots for same (event_id, position_type, alliance_id) replaces all rows
    kvk.save_slots(c, eid, "Training", 5, [
        {"slot_index": 0, "slot_time": "00:00", "fid": 7, "locked": 0},
    ])
    got_after = [r for r in kvk.get_slots(c, eid) if r["position_type"] == "Training" and r["alliance_id"] == 5]
    assert len(got_after) == 1
    assert got_after[0]["fid"] == 7

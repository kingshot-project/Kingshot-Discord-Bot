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

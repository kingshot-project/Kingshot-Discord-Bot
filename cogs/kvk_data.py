"""Pure sqlite data layer for db/kvk.sqlite (no discord import - unit-testable)."""
import contextlib
import sqlite3

_EVENT_COLS = ("id", "guild_id", "name", "event_date", "scope", "slots_per_alliance",
               "slot_mode", "signup_open_at", "signup_close_at", "publish_channel_id",
               "status", "created_by", "created_at")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kvk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
            event_date TEXT NOT NULL, scope TEXT NOT NULL, slots_per_alliance INTEGER,
            slot_mode INTEGER NOT NULL DEFAULT 0, signup_open_at TEXT NOT NULL,
            signup_close_at TEXT NOT NULL, publish_channel_id INTEGER,
            status TEXT NOT NULL DEFAULT 'collecting', created_by INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kvk_event_types (
            event_id INTEGER NOT NULL, position_type TEXT NOT NULL, type_date TEXT NOT NULL,
            PRIMARY KEY (event_id, position_type)
        );
        CREATE TABLE IF NOT EXISTS kvk_signups (
            event_id INTEGER NOT NULL, fid INTEGER NOT NULL, position_type TEXT NOT NULL,
            speedup_minutes INTEGER NOT NULL, submitted_by INTEGER NOT NULL, submitted_at TEXT NOT NULL,
            desired_slots TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (event_id, fid, position_type)
        );
        CREATE TABLE IF NOT EXISTS kvk_slots (
            event_id INTEGER NOT NULL, position_type TEXT NOT NULL,
            alliance_id INTEGER NOT NULL DEFAULT 0, slot_index INTEGER NOT NULL,
            slot_time TEXT NOT NULL, fid INTEGER, locked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_id, position_type, alliance_id, slot_index)
        );
        """
    )
    # Additive migration for databases created before desired_slots existed.
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE kvk_signups ADD COLUMN desired_slots TEXT NOT NULL DEFAULT ''")
    conn.commit()


def create_event(conn, **f) -> int:
    cur = conn.execute(
        """INSERT INTO kvk_events (guild_id, name, event_date, scope, slots_per_alliance, slot_mode,
           signup_open_at, signup_close_at, publish_channel_id, status, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'collecting', ?, ?)""",
        (f["guild_id"], f["name"], f["event_date"], f["scope"], f["slots_per_alliance"], f["slot_mode"],
         f["signup_open_at"], f["signup_close_at"], f["publish_channel_id"], f["created_by"], f["created_at"]),
    )
    conn.commit()
    return cur.lastrowid


def set_event_types(conn, event_id, types) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO kvk_event_types (event_id, position_type, type_date) VALUES (?, ?, ?)",
        [(event_id, t, d) for t, d in types],
    )
    conn.commit()


def get_active_types(conn, event_id):
    rows = conn.execute(
        "SELECT position_type FROM kvk_event_types WHERE event_id = ?", (event_id,)).fetchall()
    return [r[0] for r in rows]


def get_event_type_dates(conn, event_id):
    """(position_type, type_date) pairs for an event, ordered by date. For the menu event details."""
    rows = conn.execute(
        "SELECT position_type, type_date FROM kvk_event_types WHERE event_id = ? ORDER BY type_date, position_type",
        (event_id,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def get_event(conn, event_id):
    row = conn.execute("SELECT * FROM kvk_events WHERE id = ?", (event_id,)).fetchone()
    return dict(zip(_EVENT_COLS, row, strict=True)) if row else None


def list_events(conn, guild_id):
    """All events for one guild, newest first. Used by the /settings KvK menu picker."""
    rows = conn.execute(
        "SELECT id, name, event_date, scope, status FROM kvk_events WHERE guild_id = ? ORDER BY id DESC",
        (guild_id,)).fetchall()
    keys = ("id", "name", "event_date", "scope", "status")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def list_open_events(conn, guild_id, now):
    """Events open for signup right now: status 'collecting' and now within the signup window.

    now is a 'YYYY-MM-DD HH:MM' string, compared against signup_open_at/close_at as text.
    """
    rows = conn.execute(
        """SELECT id, name, event_date FROM kvk_events
           WHERE guild_id = ? AND status = 'collecting'
             AND signup_open_at <= ? AND ? <= signup_close_at
           ORDER BY id DESC""",
        (guild_id, now, now)).fetchall()
    keys = ("id", "name", "event_date")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def set_status(conn, event_id, status) -> None:
    conn.execute("UPDATE kvk_events SET status = ? WHERE id = ?", (status, event_id))
    conn.commit()


def delete_event(conn, event_id) -> None:
    """Remove an event and everything tied to it: its types, signups, and slots."""
    conn.execute("DELETE FROM kvk_slots WHERE event_id = ?", (event_id,))
    conn.execute("DELETE FROM kvk_signups WHERE event_id = ?", (event_id,))
    conn.execute("DELETE FROM kvk_event_types WHERE event_id = ?", (event_id,))
    conn.execute("DELETE FROM kvk_events WHERE id = ?", (event_id,))
    conn.commit()


def upsert_signup(conn, event_id, fid, position_type, speedup_minutes, submitted_by, submitted_at) -> None:
    # ON CONFLICT keeps any desired_slots already set, so re-entering speedups does not wipe them.
    conn.execute(
        """INSERT INTO kvk_signups
           (event_id, fid, position_type, speedup_minutes, submitted_by, submitted_at, desired_slots)
           VALUES (?, ?, ?, ?, ?, ?, '')
           ON CONFLICT(event_id, fid, position_type) DO UPDATE SET
             speedup_minutes = excluded.speedup_minutes,
             submitted_by = excluded.submitted_by,
             submitted_at = excluded.submitted_at""",
        (event_id, fid, position_type, speedup_minutes, submitted_by, submitted_at),
    )
    conn.commit()


def set_desired_slots(conn, event_id, fid, position_type, csv) -> None:
    """Store a signup's preferred slot indices as a comma-separated string ('' = no preference)."""
    conn.execute(
        "UPDATE kvk_signups SET desired_slots = ? WHERE event_id = ? AND fid = ? AND position_type = ?",
        (csv, event_id, fid, position_type))
    conn.commit()


def _parse_slot_csv(csv):
    return [int(x) for x in csv.split(",") if x.strip()] if csv else []


def get_signups(conn, event_id, position_type):
    rows = conn.execute(
        "SELECT fid, speedup_minutes, submitted_at, desired_slots FROM kvk_signups "
        "WHERE event_id = ? AND position_type = ?", (event_id, position_type)).fetchall()
    return [{"fid": r[0], "speedup_minutes": r[1], "submitted_at": r[2],
             "desired_slots": _parse_slot_csv(r[3])} for r in rows]


def get_signup_minutes(conn, event_id):
    rows = conn.execute(
        "SELECT fid, position_type, speedup_minutes FROM kvk_signups WHERE event_id = ?", (event_id,)).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def save_slots(conn, event_id, position_type, alliance_id, rows) -> None:
    conn.execute("DELETE FROM kvk_slots WHERE event_id = ? AND position_type = ? AND alliance_id = ?",
                 (event_id, position_type, alliance_id))
    conn.executemany(
        """INSERT INTO kvk_slots (event_id, position_type, alliance_id, slot_index, slot_time, fid, locked)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(event_id, position_type, alliance_id, r["slot_index"], r["slot_time"], r["fid"], r.get("locked", 0))
         for r in rows])
    conn.commit()


def set_slot(conn, event_id, position_type, alliance_id, slot_index, fid, locked) -> None:
    conn.execute(
        """UPDATE kvk_slots SET fid = ?, locked = ?
           WHERE event_id = ? AND position_type = ? AND alliance_id = ? AND slot_index = ?""",
        (fid, locked, event_id, position_type, alliance_id, slot_index))
    conn.commit()


def get_locks(conn, event_id, position_type, alliance_id):
    rows = conn.execute(
        """SELECT slot_index, fid FROM kvk_slots
           WHERE event_id = ? AND position_type = ? AND alliance_id = ? AND locked = 1 AND fid IS NOT NULL""",
        (event_id, position_type, alliance_id)).fetchall()
    return {r[0]: r[1] for r in rows}


def get_slots(conn, event_id):
    rows = conn.execute(
        """SELECT position_type, alliance_id, slot_index, slot_time, fid, locked
           FROM kvk_slots WHERE event_id = ? ORDER BY position_type, alliance_id, slot_index""",
        (event_id,)).fetchall()
    keys = ("position_type", "alliance_id", "slot_index", "slot_time", "fid", "locked")
    return [dict(zip(keys, r, strict=True)) for r in rows]

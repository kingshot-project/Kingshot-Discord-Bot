# KvK Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-service KvK minister-slot planner: admins create a KvK event, players submit which positions they want and how many speedups they will spend, and the bot ranks by speedups and auto-assigns players into 30-minute slots with admin override, then publishes.

**Architecture:** A standalone subsystem isolated from `minister_*`. Pure, deterministic logic (speedup parsing, slot-grid generation, ranking + assignment) lives in `cogs/kvk_util.py` and is fully unit-tested. Two Discord cogs — `cogs/kvk_scheduling.py` (event lifecycle, self-service signup, all `db/kvk.sqlite` access) and `cogs/kvk_report.py` (report, override preview, publish) — hold the Discord/IO layer. Data lives in a new `db/kvk.sqlite`.

**Tech Stack:** Python 3.12, discord.py 2.7.1, stdlib `sqlite3` (WAL), `pytest` for the pure/DB units. Discord `fid` link reuses the existing `/register` flow (`users.sqlite`).

## Global Constraints

- Python target **3.12**; no new runtime dependencies (OCR excluded — do not import onnxruntime/rapidocr).
- All new `.py` files MUST pass `ruff check` under the repo `pyproject.toml` ruleset (E,F,W,I,UP,B,C4,SIM,RUF; line-length 120). Run `ruff check <file>` before each commit.
- SQLite: additive schema only; `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`; every connection closed via `try/finally`; guard every `fetchone()` before indexing.
- `users.alliance` has **TEXT affinity** (stores the id as a string, e.g. `'1'`) while `alliance_list.alliance_id` is INTEGER — compare/group alliance ids **as strings**.
- Position types with slots are exactly: `"Training"`, `"Research"`, `"Building"`. "General" is NOT a position — it is universal speedups the player folds into a chosen position's number.
- Slot counts: `slot_mode=0` → 48 slots/day; `slot_mode=1` → 49 slots/day (two 15-min edge slots + 47 full).
- Permissions: `/kvk_create`, `/kvk_report`, `/kvk_publish`, `/kvk_edit_signup` = Global Admin; `/kvk_signup` = any registered player.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## File Structure

- Create `cogs/kvk_util.py` — pure helpers: `parse_speedups`, `generate_time_slots`, `rank_and_assign`. No discord/sqlite imports.
- Create `cogs/kvk_scheduling.py` — cog: schema init + CRUD on `db/kvk.sqlite`, `/kvk_create` wizard, `/kvk_signup`, `/kvk_edit_signup`.
- Create `cogs/kvk_report.py` — cog: `/kvk_report` (rank+assign+editable preview), `/kvk_publish`.
- Create `tests/conftest.py`, `tests/test_kvk_util.py`, `tests/test_kvk_db.py` — pytest.
- Modify the cog loader (the file that `load_extension`s `cogs/*`) to register the two new cogs.

`POSITION_TYPES = ("Training", "Research", "Building")` is defined once in `cogs/kvk_util.py` and imported everywhere.

---

## Task 1: Speedup parser (`parse_speedups`)

**Files:**
- Create: `cogs/kvk_util.py`
- Create: `tests/test_kvk_util.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Produces: `parse_speedups(text: str) -> int` (minutes). Raises `ValueError` on unparseable input. `POSITION_TYPES: tuple[str, str, str]`.

- [ ] **Step 1: Make `cogs/` importable in tests — write `tests/conftest.py`**

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cogs"))
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_kvk_util.py
import pytest
from kvk_util import parse_speedups


@pytest.mark.parametrize("text,minutes", [
    ("70h", 70 * 60),
    ("7d", 7 * 1440),
    ("7d 12h", 7 * 1440 + 12 * 60),
    ("2d4h30m", 2 * 1440 + 4 * 60 + 30),
    ("90m", 90),
    ("  3D ", 3 * 1440),
    ("1d 1h 1m", 1440 + 60 + 1),
])
def test_parse_speedups_ok(text, minutes):
    assert parse_speedups(text) == minutes


@pytest.mark.parametrize("bad", ["", "abc", "12", "5x", "d", "h m"])
def test_parse_speedups_invalid(bad):
    with pytest.raises(ValueError):
        parse_speedups(bad)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_kvk_util.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kvk_util'`.

- [ ] **Step 4: Write minimal implementation**

```python
# cogs/kvk_util.py
"""Pure, dependency-free helpers for the KvK scheduler (no discord/sqlite imports)."""
import re

POSITION_TYPES = ("Training", "Research", "Building")

_UNIT_MINUTES = {"d": 1440, "h": 60, "m": 1}
_TOKEN_RE = re.compile(r"(\d+)\s*([dhm])", re.IGNORECASE)


def parse_speedups(text: str) -> int:
    """Parse a free-form speedup duration ('7d 12h', '70h', '2d4h30m') into total minutes.

    Units: d=day(1440m), h=hour(60m), m=minute(1m). Case-insensitive, spaces optional.
    Raises ValueError if no valid unit token is found or stray characters remain.
    """
    if text is None:
        raise ValueError("empty speedup value")
    cleaned = text.strip().lower()
    if not cleaned:
        raise ValueError("empty speedup value")
    total = 0
    matched = False
    for amount, unit in _TOKEN_RE.findall(cleaned):
        total += int(amount) * _UNIT_MINUTES[unit]
        matched = True
    # Reject input that had leftover non-token characters (e.g. "5x", "12").
    if not matched or _TOKEN_RE.sub("", cleaned).strip():
        raise ValueError(f"could not parse speedups: {text!r}")
    return total
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_kvk_util.py -q`
Expected: PASS (all parametrized cases).

- [ ] **Step 6: Lint and commit**

```bash
ruff check cogs/kvk_util.py tests/
git add cogs/kvk_util.py tests/conftest.py tests/test_kvk_util.py
git commit -m "feat(kvk): speedup duration parser" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Time-slot grid (`generate_time_slots`)

**Files:**
- Modify: `cogs/kvk_util.py`
- Modify: `tests/test_kvk_util.py`

**Interfaces:**
- Produces: `generate_time_slots(slot_mode: int) -> list[str]` — `HH:MM` strings; 48 entries for mode 0, 49 for mode 1. Identical output to `MinisterSchedule.get_time_slots`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_kvk_util.py
from kvk_util import generate_time_slots


def test_slots_mode0():
    slots = generate_time_slots(0)
    assert len(slots) == 48
    assert slots[0] == "00:00"
    assert slots[1] == "00:30"
    assert slots[-1] == "23:30"


def test_slots_mode1():
    slots = generate_time_slots(1)
    assert len(slots) == 49
    assert slots[0] == "00:00"
    assert slots[1] == "00:15"
    assert slots[2] == "00:45"
    assert slots[-1] == "23:45"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_kvk_util.py -q -k slots`
Expected: FAIL — `ImportError: cannot import name 'generate_time_slots'`.

- [ ] **Step 3: Implement (append to `cogs/kvk_util.py`)**

```python
def generate_time_slots(slot_mode: int) -> list[str]:
    """Daily 30-minute slot grid. Mode 0: 00:00..23:30 (48). Mode 1: offset, two 15-min
    edge slots plus 47 full slots (49). Mirrors MinisterSchedule.get_time_slots."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_kvk_util.py -q -k slots`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check cogs/kvk_util.py tests/test_kvk_util.py
git add cogs/kvk_util.py tests/test_kvk_util.py
git commit -m "feat(kvk): daily 30-min slot grid (48/49)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Ranking + auto-assignment (`rank_and_assign`)

**Files:**
- Modify: `cogs/kvk_util.py`
- Modify: `tests/test_kvk_util.py`

**Interfaces:**
- Consumes: `generate_time_slots`.
- Produces:
  `rank_and_assign(signups, slot_count, slot_mode, locked=None) -> list[dict]`
  - `signups`: iterable of `{"fid": int, "speedup_minutes": int, "submitted_at": str}`
    (already filtered to ONE scope-unit and ONE position type by the caller).
  - `slot_count`: number of slots (N for alliance scope; 48/49 for kingdom — caller passes `len(generate_time_slots(slot_mode))`).
  - `locked`: optional `{slot_index: fid}` pins that must be preserved.
  - Returns a list of length `slot_count`: `{"slot_index": i, "slot_time": str, "fid": int | None}`, ranked by `speedup_minutes` DESC, tie-break `submitted_at` ASC then `fid` ASC. Locked fids are placed at their index and removed from the pool; remaining unlocked slots are filled in rank order; leftover slots get `fid=None`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_kvk_util.py
from kvk_util import rank_and_assign


def _s(fid, minutes, at):
    return {"fid": fid, "speedup_minutes": minutes, "submitted_at": at}


def test_assign_orders_by_speedups_desc():
    signups = [_s(1, 100, "t1"), _s(2, 300, "t2"), _s(3, 200, "t3")]
    out = rank_and_assign(signups, slot_count=3, slot_mode=0)
    assert [r["fid"] for r in out] == [2, 3, 1]
    assert out[0]["slot_time"] == "00:00"
    assert out[1]["slot_time"] == "00:30"


def test_assign_tiebreak_earlier_then_lower_fid():
    signups = [_s(5, 100, "t2"), _s(2, 100, "t2"), _s(9, 100, "t1")]
    out = rank_and_assign(signups, slot_count=3, slot_mode=0)
    assert [r["fid"] for r in out] == [9, 2, 5]  # t1 first; then equal time -> lower fid


def test_assign_caps_and_leaves_empty_slots():
    signups = [_s(1, 100, "t1")]
    out = rank_and_assign(signups, slot_count=3, slot_mode=0)
    assert [r["fid"] for r in out] == [1, None, None]


def test_assign_respects_locks():
    signups = [_s(1, 500, "t1"), _s(2, 400, "t2"), _s(3, 300, "t3")]
    out = rank_and_assign(signups, slot_count=3, slot_mode=0, locked={0: 3})
    # fid 3 pinned at slot 0; 1 and 2 fill remaining in rank order
    assert out[0]["fid"] == 3
    assert [r["fid"] for r in out[1:]] == [1, 2]


def test_assign_kingdom_49():
    slots = 49
    signups = [_s(i, 1000 - i, f"t{i:03}") for i in range(60)]
    out = rank_and_assign(signups, slot_count=slots, slot_mode=1)
    assert len(out) == 49
    assert out[0]["fid"] == 0 and out[0]["slot_time"] == "00:00"
    assert out[-1]["slot_time"] == "23:45"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_kvk_util.py -q -k assign`
Expected: FAIL — `ImportError: cannot import name 'rank_and_assign'`.

- [ ] **Step 3: Implement (append to `cogs/kvk_util.py`)**

```python
def rank_and_assign(signups, slot_count, slot_mode, locked=None):
    """Rank signups by speedups and place them into `slot_count` slots.

    signups: iterable of {"fid", "speedup_minutes", "submitted_at"} for one scope-unit+type.
    Sort: speedup_minutes DESC, then submitted_at ASC, then fid ASC.
    locked: {slot_index: fid} preserved as-is; those fids are removed from the pool.
    Returns list[{"slot_index", "slot_time", "fid"}] of length slot_count (fid may be None).
    """
    locked = dict(locked or {})
    times = generate_time_slots(slot_mode)
    ranked = sorted(
        signups,
        key=lambda s: (-s["speedup_minutes"], s["submitted_at"], s["fid"]),
    )
    locked_fids = set(locked.values())
    pool = [s["fid"] for s in ranked if s["fid"] not in locked_fids]

    result = []
    pi = 0
    for i in range(slot_count):
        slot_time = times[i] if i < len(times) else ""
        if i in locked:
            fid = locked[i]
        elif pi < len(pool):
            fid = pool[pi]
            pi += 1
        else:
            fid = None
        result.append({"slot_index": i, "slot_time": slot_time, "fid": fid})
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_kvk_util.py -q`
Expected: PASS (all util tests).

- [ ] **Step 5: Lint and commit**

```bash
ruff check cogs/kvk_util.py tests/test_kvk_util.py
git add cogs/kvk_util.py tests/test_kvk_util.py
git commit -m "feat(kvk): ranking + slot auto-assignment" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Data layer — schema + CRUD (`db/kvk.sqlite`)

**Files:**
- Create: `cogs/kvk_scheduling.py` (data-layer portion only in this task)
- Create: `tests/test_kvk_db.py`

**Interfaces:**
- Produces a module-level, testable DB API in `cogs/kvk_scheduling.py` that accepts an explicit `conn` so tests can pass an in-memory connection:
  - `init_schema(conn)` — create the 4 tables (idempotent).
  - `create_event(conn, **fields) -> int` (returns event id).
  - `set_event_types(conn, event_id, types)` — `types` = list of `(position_type, type_date)`.
  - `upsert_signup(conn, event_id, fid, position_type, speedup_minutes, submitted_by, submitted_at)`.
  - `get_signups(conn, event_id, position_type) -> list[dict]` (`fid, speedup_minutes, submitted_at`).
  - `save_slots(conn, event_id, position_type, alliance_id, rows)` — replace that group's slots with `rows` from `rank_and_assign`.
  - `get_slots(conn, event_id) -> list[dict]`.
  - `set_slot(conn, event_id, position_type, alliance_id, slot_index, fid, locked)` — admin override.
  - `set_status(conn, event_id, status)` / `get_event(conn, event_id) -> dict | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kvk_db.py
import sqlite3
import importlib

kvk = importlib.import_module("kvk_scheduling")


def _conn():
    c = sqlite3.connect(":memory:")
    kvk.init_schema(c)
    return c


def test_create_event_and_types():
    c = _conn()
    eid = kvk.create_event(
        c, guild_id=1, name="KvK 42", event_date="2026-09-01", scope="alliance",
        slots_per_alliance=5, slot_mode=0, signup_open_at="2026-08-20T00:00",
        signup_close_at="2026-08-31T00:00", publish_channel_id=99, created_by=7,
        created_at="2026-08-19T00:00",
    )
    assert isinstance(eid, int)
    kvk.set_event_types(c, eid, [("Training", "2026-09-01"), ("Research", "2026-09-02")])
    ev = kvk.get_event(c, eid)
    assert ev["name"] == "KvK 42" and ev["status"] == "collecting"


def test_signup_upsert_is_idempotent():
    c = _conn()
    eid = kvk.create_event(
        c, guild_id=1, name="K", event_date="2026-09-01", scope="kingdom",
        slots_per_alliance=None, slot_mode=1, signup_open_at="a", signup_close_at="b",
        publish_channel_id=None, created_by=1, created_at="c",
    )
    kvk.upsert_signup(c, eid, fid=100, position_type="Training",
                      speedup_minutes=600, submitted_by=100, submitted_at="t1")
    kvk.upsert_signup(c, eid, fid=100, position_type="Training",
                      speedup_minutes=900, submitted_by=100, submitted_at="t2")
    rows = kvk.get_signups(c, eid, "Training")
    assert rows == [{"fid": 100, "speedup_minutes": 900, "submitted_at": "t2"}]


def test_save_and_override_slots():
    c = _conn()
    eid = kvk.create_event(
        c, guild_id=1, name="K", event_date="d", scope="alliance",
        slots_per_alliance=2, slot_mode=0, signup_open_at="a", signup_close_at="b",
        publish_channel_id=None, created_by=1, created_at="c",
    )
    rows = [
        {"slot_index": 0, "slot_time": "00:00", "fid": 1},
        {"slot_index": 1, "slot_time": "00:30", "fid": None},
    ]
    kvk.save_slots(c, eid, "Training", alliance_id=5, rows=rows)
    kvk.set_slot(c, eid, "Training", alliance_id=5, slot_index=1, fid=42, locked=1)
    got = {(r["slot_index"], r["fid"], r["locked"]) for r in kvk.get_slots(c, eid)}
    assert (0, 1, 0) in got and (1, 42, 1) in got
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_kvk_db.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kvk_scheduling'` (or missing `init_schema`).

- [ ] **Step 3: Implement the data layer at the top of `cogs/kvk_scheduling.py`**

```python
"""KvK scheduler: event lifecycle, self-service signup, and all db/kvk.sqlite access."""
import os
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from kvk_util import POSITION_TYPES, parse_speedups  # noqa: F401  (used by later tasks)

DB_PATH = "db/kvk.sqlite"


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kvk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            event_date TEXT NOT NULL,
            scope TEXT NOT NULL,
            slots_per_alliance INTEGER,
            slot_mode INTEGER NOT NULL DEFAULT 0,
            signup_open_at TEXT NOT NULL,
            signup_close_at TEXT NOT NULL,
            publish_channel_id INTEGER,
            status TEXT NOT NULL DEFAULT 'collecting',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kvk_event_types (
            event_id INTEGER NOT NULL,
            position_type TEXT NOT NULL,
            type_date TEXT NOT NULL,
            PRIMARY KEY (event_id, position_type)
        );
        CREATE TABLE IF NOT EXISTS kvk_signups (
            event_id INTEGER NOT NULL,
            fid INTEGER NOT NULL,
            position_type TEXT NOT NULL,
            speedup_minutes INTEGER NOT NULL,
            submitted_by INTEGER NOT NULL,
            submitted_at TEXT NOT NULL,
            PRIMARY KEY (event_id, fid, position_type)
        );
        CREATE TABLE IF NOT EXISTS kvk_slots (
            event_id INTEGER NOT NULL,
            position_type TEXT NOT NULL,
            alliance_id INTEGER NOT NULL DEFAULT 0,
            slot_index INTEGER NOT NULL,
            slot_time TEXT NOT NULL,
            fid INTEGER,
            locked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_id, position_type, alliance_id, slot_index)
        );
        """
    )
    conn.commit()


def create_event(conn, **f) -> int:
    cur = conn.execute(
        """INSERT INTO kvk_events
           (guild_id, name, event_date, scope, slots_per_alliance, slot_mode,
            signup_open_at, signup_close_at, publish_channel_id, status, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'collecting', ?, ?)""",
        (f["guild_id"], f["name"], f["event_date"], f["scope"], f["slots_per_alliance"],
         f["slot_mode"], f["signup_open_at"], f["signup_close_at"], f["publish_channel_id"],
         f["created_by"], f["created_at"]),
    )
    conn.commit()
    return cur.lastrowid


def set_event_types(conn, event_id, types) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO kvk_event_types (event_id, position_type, type_date) VALUES (?, ?, ?)",
        [(event_id, t, d) for t, d in types],
    )
    conn.commit()


def get_event(conn, event_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM kvk_events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def set_status(conn, event_id, status) -> None:
    conn.execute("UPDATE kvk_events SET status = ? WHERE id = ?", (status, event_id))
    conn.commit()


def upsert_signup(conn, event_id, fid, position_type, speedup_minutes, submitted_by, submitted_at) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO kvk_signups
           (event_id, fid, position_type, speedup_minutes, submitted_by, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_id, fid, position_type, speedup_minutes, submitted_by, submitted_at),
    )
    conn.commit()


def get_signups(conn, event_id, position_type):
    rows = conn.execute(
        """SELECT fid, speedup_minutes, submitted_at FROM kvk_signups
           WHERE event_id = ? AND position_type = ?""",
        (event_id, position_type),
    ).fetchall()
    return [{"fid": r[0], "speedup_minutes": r[1], "submitted_at": r[2]} for r in rows]


def save_slots(conn, event_id, position_type, alliance_id, rows) -> None:
    conn.execute(
        "DELETE FROM kvk_slots WHERE event_id = ? AND position_type = ? AND alliance_id = ?",
        (event_id, position_type, alliance_id),
    )
    conn.executemany(
        """INSERT INTO kvk_slots
           (event_id, position_type, alliance_id, slot_index, slot_time, fid, locked)
           VALUES (?, ?, ?, ?, ?, ?, 0)""",
        [(event_id, position_type, alliance_id, r["slot_index"], r["slot_time"], r["fid"]) for r in rows],
    )
    conn.commit()


def set_slot(conn, event_id, position_type, alliance_id, slot_index, fid, locked) -> None:
    conn.execute(
        """UPDATE kvk_slots SET fid = ?, locked = ?
           WHERE event_id = ? AND position_type = ? AND alliance_id = ? AND slot_index = ?""",
        (fid, locked, event_id, position_type, alliance_id, slot_index),
    )
    conn.commit()


def get_slots(conn, event_id):
    rows = conn.execute(
        """SELECT position_type, alliance_id, slot_index, slot_time, fid, locked
           FROM kvk_slots WHERE event_id = ? ORDER BY position_type, alliance_id, slot_index""",
        (event_id,),
    ).fetchall()
    keys = ("position_type", "alliance_id", "slot_index", "slot_time", "fid", "locked")
    return [dict(zip(keys, r)) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_kvk_db.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check cogs/kvk_scheduling.py tests/test_kvk_db.py
git add cogs/kvk_scheduling.py tests/test_kvk_db.py
git commit -m "feat(kvk): db/kvk.sqlite schema and CRUD" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: Cog scaffold + `/kvk_create` wizard

**Files:**
- Modify: `cogs/kvk_scheduling.py` (add the `KvkScheduling(commands.Cog)` class)

**Interfaces:**
- Consumes: the Task-4 data API, `POSITION_TYPES`.
- Produces: `class KvkScheduling(commands.Cog)` with `self.conn` (a persistent `db/kvk.sqlite` connection opened in `__init__` via `init_schema`); an `async def setup(bot)` at module end that does `await bot.add_cog(KvkScheduling(bot))`. A Global-Admin check helper `_is_global_admin(interaction) -> bool` reused by later tasks.

- [ ] **Step 1: Add the cog class, admin check, and connection lifecycle**

```python
class KvkScheduling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        os.makedirs("db", exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
        init_schema(self.conn)

    async def cog_unload(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _is_global_admin(self, interaction: discord.Interaction) -> bool:
        # Mirror the project's existing Global-Admin gate used by minister_* commands.
        settings = sqlite3.connect("db/settings.sqlite")
        try:
            row = settings.execute(
                "SELECT 1 FROM admin WHERE id = ? AND is_initial = 1",
                (interaction.user.id,),
            ).fetchone()
            return row is not None
        finally:
            settings.close()
```

Note: confirm the exact Global-Admin query against `cogs/permission_handler.py` before implementing — reuse that helper if one exists rather than duplicating SQL.

- [ ] **Step 2: Add `/kvk_create` as a modal-driven wizard**

Implement `@app_commands.command(name="kvk_create")`. Because a single Discord modal is limited to 5 text inputs, collect core fields in one modal, then use a follow-up `discord.ui.View` with selects for `scope`, `slot_mode`, and the active position types + a per-type date modal. Concretely:

```python
    @app_commands.command(name="kvk_create", description="Create a KvK event (Global Admin).")
    async def kvk_create(self, interaction: discord.Interaction):
        if not self._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(_KvkCreateModal(self))
```

`_KvkCreateModal` collects: `name`, `event_date` (YYYY-MM-DD), `signup_open_at`, `signup_close_at`, `slots_per_alliance` (blank for kingdom). On submit, validate dates with `datetime.strptime`, then send an ephemeral `_KvkCreateOptions` view (selects: scope [alliance/kingdom], slot_mode [0/1], position types [multi]; a "Set dates & channel" button opens a modal for each chosen type's `type_date` and the publish channel). On final confirm, call `create_event(...)` + `set_event_types(...)`, then post an announcement embed in the publish channel telling players to run `/kvk_signup`.

Full modal/view code is written during implementation following the existing modal/view patterns in `cogs/minister_menu.py` (e.g. `discord.ui.Modal`, `discord.ui.Select`, `discord.ui.Button`). Keep the file under ~600 lines; if it grows past that, split the wizard views into `cogs/kvk_scheduling_views.py`.

- [ ] **Step 3: Manual verification**

Run the bot locally against a test guild:
- `/kvk_create` as a Global Admin → complete the wizard → verify a row appears: `sqlite3 db/kvk.sqlite "SELECT * FROM kvk_events;"` and the announcement is posted.
- `/kvk_create` as a non-admin → verify "Global Admin only."

- [ ] **Step 4: Lint and commit**

```bash
ruff check cogs/kvk_scheduling.py
git add cogs/kvk_scheduling.py
git commit -m "feat(kvk): /kvk_create wizard and cog scaffold" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: `/kvk_signup` (self-service) + `/kvk_edit_signup`

**Files:**
- Modify: `cogs/kvk_scheduling.py`

**Interfaces:**
- Consumes: `parse_speedups`, `get_event`, `upsert_signup`, `POSITION_TYPES`; the existing `/register` link table `users(fid, discord_id, ...)` in `db/users.sqlite`.
- Produces: `_fid_for_discord(discord_id) -> int | None` helper.

- [ ] **Step 1: Add the fid-resolver helper**

```python
    def _fid_for_discord(self, discord_id: int):
        users = sqlite3.connect("db/users.sqlite")
        try:
            row = users.execute(
                "SELECT fid FROM users WHERE discord_id = ? ORDER BY fid LIMIT 1",
                (discord_id,),
            ).fetchone()
            return row[0] if row else None
        finally:
            users.close()
```

- [ ] **Step 2: Implement `/kvk_signup`**

```python
    @app_commands.command(name="kvk_signup", description="Submit your KvK positions and speedups.")
    @app_commands.describe(event_id="KvK event id")
    async def kvk_signup(self, interaction: discord.Interaction, event_id: int):
        fid = self._fid_for_discord(interaction.user.id)
        if fid is None:
            await interaction.response.send_message(
                "You are not registered. Run /register first.", ephemeral=True)
            return
        ev = get_event(self.conn, event_id)
        if not ev or ev["status"] != "collecting":
            await interaction.response.send_message("This event is not open for signups.", ephemeral=True)
            return
        now = datetime.now(UTC).isoformat()
        if not (ev["signup_open_at"] <= now <= ev["signup_close_at"]):
            await interaction.response.send_message("The signup window is closed.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Pick your positions:", view=_SignupView(self, event_id, fid), ephemeral=True)
```

Add at the top of the file: `from datetime import datetime, timezone as _tz` and `UTC = _tz.utc` (or `from datetime import datetime, UTC` on py3.12). `_SignupView` shows a multi-select of `POSITION_TYPES` (max 3); on selection it opens a modal with one speedup text input per chosen type. On modal submit: `mins = parse_speedups(value)` for each (catch `ValueError` -> ephemeral error, write nothing), then `upsert_signup(self.conn, event_id, fid, ptype, mins, interaction.user.id, datetime.now(UTC).isoformat())` per type; confirm ephemerally.

- [ ] **Step 3: Implement `/kvk_edit_signup` (admin)**

Same flow as `/kvk_signup` but takes an explicit `fid` parameter, requires `_is_global_admin`, and stamps `submitted_by = interaction.user.id`.

- [ ] **Step 4: Manual verification**

- Registered player: `/kvk_signup <event_id>` → choose Training + Research, enter `7d` and `70h` → verify two rows and minutes: `sqlite3 db/kvk.sqlite "SELECT fid,position_type,speedup_minutes FROM kvk_signups;"`.
- Re-run and change Training to `10d` → verify the row updated (idempotent), not duplicated.
- Unregistered user → "run /register first". Signup after `signup_close_at` → "window is closed".
- Enter `abc` for speedups → ephemeral parse error, no row written.

- [ ] **Step 5: Lint and commit**

```bash
ruff check cogs/kvk_scheduling.py
git add cogs/kvk_scheduling.py
git commit -m "feat(kvk): self-service /kvk_signup and admin /kvk_edit_signup" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: `/kvk_report` — rank, assign, editable preview

**Files:**
- Create: `cogs/kvk_report.py`

**Interfaces:**
- Consumes: `rank_and_assign`, `generate_time_slots` (from `kvk_util`); `get_event`, `get_signups`, `save_slots`, `get_slots`, `set_slot`, `set_status` (from `kvk_scheduling`); `users.sqlite` for `fid -> alliance/nickname` (alliance compared as strings).
- Produces: `class KvkReport(commands.Cog)`; `_compute_assignments(event_id)` that fills `kvk_slots` for every (scope-unit, active type).

- [ ] **Step 1: Implement the assignment driver**

```python
"""KvK report: rank submissions, auto-assign slots, allow admin override, publish."""
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

import kvk_scheduling as kvkdb
from kvk_util import generate_time_slots, rank_and_assign


def _alliance_of(fid: int) -> str | None:
    users = sqlite3.connect("db/users.sqlite")
    try:
        row = users.execute("SELECT alliance FROM users WHERE fid = ?", (fid,)).fetchone()
        return str(row[0]) if row and row[0] is not None else None  # TEXT affinity: keep as str
    finally:
        users.close()


class KvkReport(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _compute(self, conn, event_id):
        ev = kvkdb.get_event(conn, event_id)
        if not ev:
            return False
        types = [r["position_type"] for r in conn.execute(
            "SELECT position_type FROM kvk_event_types WHERE event_id = ?", (event_id,)).fetchall()]
        slot_mode = ev["slot_mode"]
        full_day = len(generate_time_slots(slot_mode))
        for ptype in types:
            signups = kvkdb.get_signups(conn, event_id, ptype)
            if ev["scope"] == "kingdom":
                rows = rank_and_assign(signups, full_day, slot_mode)
                kvkdb.save_slots(conn, event_id, ptype, 0, rows)
            else:
                n = ev["slots_per_alliance"] or 0
                by_all = {}
                for s in signups:
                    aid = _alliance_of(s["fid"])
                    if aid is None:
                        continue  # orphan: excluded
                    by_all.setdefault(aid, []).append(s)
                for aid, group in by_all.items():
                    rows = rank_and_assign(group, n, slot_mode)
                    kvkdb.save_slots(conn, event_id, ptype, int(aid), rows)
        kvkdb.set_status(conn, event_id, "assigned")
        return True
```

- [ ] **Step 2: Implement `/kvk_report` command + preview view**

```python
    @app_commands.command(name="kvk_report", description="Rank and assign KvK slots (Global Admin).")
    async def kvk_report(self, interaction: discord.Interaction, event_id: int):
        sched = self.bot.get_cog("KvkScheduling")
        if sched is None or not sched._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        self._compute(sched.conn, event_id)
        embed = self._render(sched.conn, event_id)
        await interaction.response.send_message(
            embed=embed, view=_OverrideView(self, sched, event_id), ephemeral=True)
```

`_render(conn, event_id)` builds an embed grouping `get_slots` by `position_type` then `alliance_id`, each line `slot_time — nickname (fid) [Xh]` or `— empty —`, with a lock marker. `_OverrideView` offers buttons: **Swap** (modal: two slot indices within a group), **Lock/Unlock** (toggle `locked` via `set_slot`), **Clear slot** (`set_slot fid=None`), **Re-run** (`_compute`, honoring locked rows — see note), and **Publish** (delegates to Task 8). Nicknames come from `users.sqlite`.

Note on re-run honoring locks: `_compute` must read existing locked rows first and pass them as `locked={slot_index: fid}` into `rank_and_assign` per group (extend `_compute` to load locks before overwriting). Implement this in Step 1's function once the override view exists.

- [ ] **Step 3: Manual verification**

- Seed signups across two alliances (alliance scope, N=2). Run `/kvk_report` → verify each alliance's top-2 by speedups fill slots, ties broken by earlier submit, empty slots shown when <2.
- Switch a test event to kingdom scope → verify 48 (mode 0) / 49 (mode 1) slots and kingdom-wide ranking.
- Lock a slot, Re-run → verify the locked fid stays put.

- [ ] **Step 4: Lint and commit**

```bash
ruff check cogs/kvk_report.py
git add cogs/kvk_report.py
git commit -m "feat(kvk): /kvk_report ranking, assignment, and override preview" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: `/kvk_publish`

**Files:**
- Modify: `cogs/kvk_report.py`

**Interfaces:**
- Consumes: `get_event`, `get_slots`, `set_status`; `publish_channel_id`.

- [ ] **Step 1: Implement publish**

```python
    @app_commands.command(name="kvk_publish", description="Publish the KvK schedule (Global Admin).")
    async def kvk_publish(self, interaction: discord.Interaction, event_id: int):
        sched = self.bot.get_cog("KvkScheduling")
        if sched is None or not sched._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        ev = kvkdb.get_event(sched.conn, event_id)
        if not ev or not ev["publish_channel_id"]:
            await interaction.response.send_message("No publish channel set.", ephemeral=True)
            return
        channel = self.bot.get_channel(int(ev["publish_channel_id"]))
        if channel is None:
            await interaction.response.send_message("Publish channel not found.", ephemeral=True)
            return
        embed = self._render(sched.conn, event_id)
        embed.title = f"KvK Schedule — {ev['name']}"
        await channel.send(embed=embed)
        kvkdb.set_status(sched.conn, event_id, "published")
        await interaction.response.send_message("Published.", ephemeral=True)
```

- [ ] **Step 2: Manual verification**

- `/kvk_publish <event_id>` → the schedule embed appears in the configured channel; `SELECT status FROM kvk_events` shows `published`.
- Missing/invalid channel → friendly ephemeral error, nothing posted.

- [ ] **Step 3: Lint and commit**

```bash
ruff check cogs/kvk_report.py
git add cogs/kvk_report.py
git commit -m "feat(kvk): /kvk_publish schedule to channel" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 9: Register cogs + full smoke test

**Files:**
- Modify: the loader that `load_extension`s cogs (find via `grep -rn "load_extension" main.py cogs`).

**Interfaces:** none new.

- [ ] **Step 1: Add `setup()` hooks**

Ensure each cog file ends with:

```python
async def setup(bot):
    await bot.add_cog(KvkScheduling(bot))   # kvk_scheduling.py
```
```python
async def setup(bot):
    await bot.add_cog(KvkReport(bot))       # kvk_report.py
```

If the loader auto-discovers `cogs/*.py`, confirm `kvk_util.py` (no cog) is skipped or has no `setup`. If the loader uses an explicit list, add `cogs.kvk_scheduling` and `cogs.kvk_report`.

- [ ] **Step 2: Full local smoke test**

Boot the bot with the OCR-free venv. Verify in logs: modules loaded count increased and no import error; `/kvk_*` commands appear after sync. Walk the end-to-end flow: create → signup (2 players) → report → override → publish. Confirm `db/kvk.sqlite` has rows in all four tables.

- [ ] **Step 3: Run the whole test suite + lint**

```bash
python -m pytest tests/ -q
ruff check cogs/kvk_util.py cogs/kvk_scheduling.py cogs/kvk_report.py tests/
```
Expected: all tests PASS, ruff clean.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat(kvk): register KvK cogs in the loader" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- Do NOT edit `minister_*`. KvK is isolated; only the cog loader is touched.
- Follow existing modal/view idioms in `cogs/minister_menu.py` and `cogs/notification_wizard.py` for the wizard/preview UI.
- Discord UI cannot be unit-tested here; Tasks 5-8 rely on manual verification. The deterministic core (Tasks 1-4) carries the automated tests — keep all logic that can live in `kvk_util`/the data layer there, not in the cogs.
- After each task, the branch must stay green: `python -m pytest tests/ -q` and `ruff check` on changed files.

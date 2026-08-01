# KvK Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-service KvK minister-slot planner: admins create a KvK event, players submit which positions they want and how many speedups they will spend, and the bot ranks by speedups and auto-assigns players into 30-minute slots with admin override, then publishes.

**Architecture:** A standalone subsystem isolated from `minister_*`. Pure, deterministic logic (speedup parsing, slot-grid generation, ranking + assignment) lives in `cogs/kvk_util.py`; the `db/kvk.sqlite` data layer lives in `cogs/kvk_data.py` — **both are discord-free and fully unit-tested**. Two Discord cogs — `cogs/kvk_scheduling.py` (event lifecycle, self-service signup) and `cogs/kvk_report.py` (report, override, publish) — hold the Discord/IO layer only.

**Tech Stack:** Python 3.12, discord.py 2.7.1, stdlib `sqlite3` (WAL), `pytest` for the pure/data units (dev-only, NOT added to `requirements.txt`; install with `pip install pytest`). Discord `fid` link reuses the existing `/register` flow (`users.sqlite`).

## Global Constraints

- Python target **3.12**; no new runtime dependencies (OCR excluded — do not import onnxruntime/rapidocr). `pytest` is dev-only.
- **Import convention (repo has NO `cogs/__init__.py`):** cogs import siblings **relatively** (`from .kvk_util import ...`, `from . import kvk_data as kvkdb`) — this is how every existing cog works and how `main.py` loads `cogs.<name>`. Tests import via the namespace package (`from cogs.kvk_util import ...`) with the **repo root** on `sys.path` (see `tests/conftest.py`).
- All new `.py` files MUST pass `ruff check` under the repo `pyproject.toml` ruleset (E,F,W,I,UP,B,C4,SIM,RUF; line-length 120). Keep imports sorted (I001), no bare `except: pass` (use `contextlib.suppress`), `zip(..., strict=...)` (B905). Run `ruff check <file>` before each commit.
- SQLite: additive schema only; `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`; every connection closed via `try/finally`; guard every `fetchone()` before indexing.
- `users.alliance` has **TEXT affinity** (stores the id as a string, e.g. `'1'`) while `alliance_list.alliance_id` is INTEGER — compare/group alliance ids **as strings**; a non-numeric value is skipped, not crashed.
- Position types with slots are exactly: `"Training"`, `"Research"`, `"Building"`. "General" is NOT a position — it is universal speedups the player folds into a chosen position's number.
- Slot counts: `slot_mode=0` → 48 slots/day; `slot_mode=1` → 49 slots/day.
- Datetimes are stored and compared in one pinned format: **`YYYY-MM-DD HH:MM` UTC**, validated with `datetime.strptime`. Never compare unpinned strings.
- Permissions: `/kvk_create`, `/kvk_report`, `/kvk_publish`, `/kvk_edit_signup` = Global Admin; `/kvk_signup` = any registered player. Reuse `PermissionManager.is_admin(user_id) -> (is_admin, is_global)` from `cogs/permission_handler.py` (do not hand-roll the SQL).
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## File Structure

- Create `cogs/kvk_util.py` — pure: `parse_speedups`, `generate_time_slots`, `rank_and_assign`, `POSITION_TYPES`. No discord/sqlite imports.
- Create `cogs/kvk_data.py` — pure sqlite data layer for `db/kvk.sqlite`: `init_schema` + CRUD. No discord import.
- Create `cogs/kvk_scheduling.py` — cog: `/kvk_create` wizard, `/kvk_signup`, `/kvk_edit_signup`. Holds the persistent connection and admin check.
- Create `cogs/kvk_report.py` — cog: `/kvk_report` (rank+assign+editable preview), `/kvk_publish`.
- Create `tests/conftest.py`, `tests/test_kvk_util.py`, `tests/test_kvk_data.py` — pytest.
- Modify `main.py` cog list (`main.py:1499`, loaded as `cogs.<name>` at `:1512`) — add bare names `"kvk_scheduling"`, `"kvk_report"`.

---

## Task 1: Speedup parser + slot grid (`cogs/kvk_util.py`)

**Files:**
- Create: `cogs/kvk_util.py`, `tests/test_kvk_util.py`, `tests/conftest.py`

**Interfaces:**
- Produces: `parse_speedups(text) -> int` (minutes, raises ValueError); `generate_time_slots(slot_mode) -> list[str]` (48 for mode 0, 49 for mode 1); `POSITION_TYPES`.

- [ ] **Step 1: `tests/conftest.py` puts the repo ROOT on the path**

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_kvk_util.py
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
```

- [ ] **Step 3: Run — expect FAIL** (`ModuleNotFoundError: No module named 'cogs.kvk_util'`)

Run: `python -m pytest tests/test_kvk_util.py -q`

- [ ] **Step 4: Implement `cogs/kvk_util.py`**

```python
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
```

(`rank_and_assign` is added in Task 2; the Task-1 test imports only the two functions defined here, so the suite collects and passes now.)

- [ ] **Step 5: Run the tests — expect PASS**

Run: `python -m pytest tests/test_kvk_util.py -q`

- [ ] **Step 6: Lint & commit**

```bash
ruff check cogs/kvk_util.py tests/
git add cogs/kvk_util.py tests/conftest.py tests/test_kvk_util.py
git commit -m "feat(kvk): speedup parser and slot grid" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Ranking + auto-assignment (`rank_and_assign`)

**Files:**
- Modify: `cogs/kvk_util.py`, `tests/test_kvk_util.py`

**Interfaces:**
- Consumes: `generate_time_slots`.
- Produces: `rank_and_assign(signups, slot_count, slot_mode, locked=None) -> list[dict]`.
  - `signups`: iterable of `{"fid", "speedup_minutes", "submitted_at"}` (already filtered to one scope-unit + one type).
  - `locked`: `{slot_index: fid}` pinned and removed from the pool.
  - Returns length-`slot_count` list of `{"slot_index", "slot_time", "fid", "locked"}` (fid may be None). Sort: `speedup_minutes` DESC, `submitted_at` ASC, `fid` ASC. Locked rows carry `locked=1`; others `locked=0`.

- [ ] **Step 1: Add `rank_and_assign` to the import, then append failing tests to `tests/test_kvk_util.py`**

First change the import line at the top of `tests/test_kvk_util.py` to:
`from cogs.kvk_util import generate_time_slots, parse_speedups, rank_and_assign`
Then append:

```python
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
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError: cannot import name 'rank_and_assign'`)

- [ ] **Step 3: Append implementation to `cogs/kvk_util.py`**

```python
def rank_and_assign(signups, slot_count, slot_mode, locked=None):
    """Rank signups by speedups and place them into slot_count slots (see module docstring)."""
    locked = dict(locked or {})
    times = generate_time_slots(slot_mode)
    ranked = sorted(signups, key=lambda s: (-s["speedup_minutes"], s["submitted_at"], s["fid"]))
    locked_fids = set(locked.values())
    pool = [s["fid"] for s in ranked if s["fid"] not in locked_fids]
    result = []
    pi = 0
    for i in range(slot_count):
        slot_time = times[i] if i < len(times) else ""
        if i in locked:
            result.append({"slot_index": i, "slot_time": slot_time, "fid": locked[i], "locked": 1})
        elif pi < len(pool):
            result.append({"slot_index": i, "slot_time": slot_time, "fid": pool[pi], "locked": 0})
            pi += 1
        else:
            result.append({"slot_index": i, "slot_time": slot_time, "fid": None, "locked": 0})
    return result
```

- [ ] **Step 4: Run full util suite — expect PASS**

Run: `python -m pytest tests/test_kvk_util.py -q`

- [ ] **Step 5: Lint & commit**

```bash
ruff check cogs/kvk_util.py tests/test_kvk_util.py
git add cogs/kvk_util.py tests/test_kvk_util.py
git commit -m "feat(kvk): ranking + slot auto-assignment with locks" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Data layer (`cogs/kvk_data.py`, discord-free)

**Files:**
- Create: `cogs/kvk_data.py`, `tests/test_kvk_data.py`

**Interfaces (all take an explicit `conn` so tests use `:memory:`):**
`init_schema(conn)`, `create_event(conn, **fields) -> int`, `set_event_types(conn, event_id, types)`, `get_active_types(conn, event_id) -> list[str]`, `get_event(conn, event_id) -> dict | None`, `set_status(conn, event_id, status)`, `upsert_signup(conn, ...)`, `get_signups(conn, event_id, position_type) -> list[dict]`, `get_signup_minutes(conn, event_id) -> dict[(fid,type)->minutes]`, `save_slots(conn, event_id, position_type, alliance_id, rows)`, `set_slot(conn, ..., slot_index, fid, locked)`, `get_locks(conn, event_id, position_type, alliance_id) -> dict[index->fid]`, `get_slots(conn, event_id) -> list[dict]`.

- [ ] **Step 1: Write `tests/test_kvk_data.py`**

```python
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
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: No module named 'cogs.kvk_data'`)

- [ ] **Step 3: Implement `cogs/kvk_data.py`**

```python
"""Pure sqlite data layer for db/kvk.sqlite (no discord import — unit-testable)."""
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


def get_event(conn, event_id):
    row = conn.execute("SELECT * FROM kvk_events WHERE id = ?", (event_id,)).fetchone()
    return dict(zip(_EVENT_COLS, row)) if row else None


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
        "SELECT fid, speedup_minutes, submitted_at FROM kvk_signups WHERE event_id = ? AND position_type = ?",
        (event_id, position_type)).fetchall()
    return [{"fid": r[0], "speedup_minutes": r[1], "submitted_at": r[2]} for r in rows]


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
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_kvk_data.py -q`

- [ ] **Step 5: Lint & commit**

```bash
ruff check cogs/kvk_data.py tests/test_kvk_data.py
git add cogs/kvk_data.py tests/test_kvk_data.py
git commit -m "feat(kvk): db/kvk.sqlite schema and CRUD" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Cog scaffold + `/kvk_create` wizard (`cogs/kvk_scheduling.py`)

**Files:** Create `cogs/kvk_scheduling.py`.

**Interfaces:** `class KvkScheduling(commands.Cog)` with `self.conn` (opened in `__init__`, `init_schema`); `_is_global_admin(interaction) -> bool`; module-end `async def setup(bot)`.

- [ ] **Step 1: Scaffold, imports (RELATIVE), admin check, lifecycle**

```python
"""KvK scheduler cog: event lifecycle and self-service signup."""
import contextlib
import os
import sqlite3
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from . import kvk_data as kvkdb
from .kvk_util import POSITION_TYPES, parse_speedups
from .permission_handler import PermissionManager

DB_PATH = "db/kvk.sqlite"
DT_FMT = "%Y-%m-%d %H:%M"


class KvkScheduling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        os.makedirs("db", exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
        kvkdb.init_schema(self.conn)

    async def cog_unload(self):
        with contextlib.suppress(Exception):
            self.conn.close()

    def _is_global_admin(self, interaction: discord.Interaction) -> bool:
        is_admin, is_global = PermissionManager.is_admin(interaction.user.id)
        return bool(is_admin and is_global)


async def setup(bot):
    await bot.add_cog(KvkScheduling(bot))
```

Verify `PermissionManager.is_admin`'s exact return shape against `cogs/permission_handler.py:55` before wiring; adapt the unpack if it differs.

- [ ] **Step 2: `/kvk_create` wizard**

`@app_commands.command(name="kvk_create")`, Global-Admin gated. Because one modal is capped at 5 inputs, split into: (a) `_KvkCreateModal` collecting `name`, `event_date` (`YYYY-MM-DD`), `signup_open_at` / `signup_close_at` (both `YYYY-MM-DD HH:MM` UTC — validate all with `datetime.strptime`; on bad format, ephemeral error, abort), `slots_per_alliance` (blank => kingdom scope); (b) a follow-up ephemeral `discord.ui.View` with a `slot_mode` select (0/1), a multi-select of `POSITION_TYPES` (active types), a `discord.ui.ChannelSelect` for the publish channel, and a per-active-type date modal (`type_date` = `YYYY-MM-DD`). Validation rules to enforce here:
- If alliance scope: `slots_per_alliance` is a positive int `<= len(generate_time_slots(slot_mode))`; else error.
On final confirm: `kvkdb.create_event(...)` → `event_id`; `kvkdb.set_event_types(event_id, [(type, type_date), ...])`; post an announcement embed **including the numeric `event_id`** in the chosen publish channel ("Run `/kvk_signup event_id:<id>`"). Build the wizard views following `cogs/minister_menu.py` / `cogs/notification_wizard.py` idioms. If the file exceeds ~600 lines, move views into `cogs/kvk_scheduling_views.py`.

- [ ] **Step 3: Manual verification** — `/kvk_create` as admin completes; `sqlite3 db/kvk.sqlite "SELECT id,name,scope,slot_mode,status FROM kvk_events;"` shows the row; announcement posted with the id. Non-admin → "Global Admin only". Bad date format → ephemeral error, no row. N greater than the grid → rejected.

- [ ] **Step 4: Lint & commit** (`ruff check cogs/kvk_scheduling.py`; commit `feat(kvk): /kvk_create wizard and cog scaffold`).

---

## Task 5: `/kvk_signup` (self-service) + `/kvk_edit_signup`

**Files:** Modify `cogs/kvk_scheduling.py`.

**Interfaces:** `_fid_for_discord(discord_id) -> int | None`.

- [ ] **Step 1: fid resolver** (note: a user may have multiple registered fids; if `>1`, present a select so they pick — do not silently take the lowest)

```python
    def _fids_for_discord(self, discord_id: int) -> list[int]:
        users = sqlite3.connect("db/users.sqlite")
        try:
            rows = users.execute("SELECT fid FROM users WHERE discord_id = ? ORDER BY fid", (discord_id,)).fetchall()
            return [r[0] for r in rows]
        finally:
            users.close()
```

- [ ] **Step 2: `/kvk_signup event_id:int`**

Flow: resolve fids (empty → "run /register first"); `ev = kvkdb.get_event(self.conn, event_id)` (None or `status != 'collecting'` → "not open"); window check with parsed datetimes:
```python
now = datetime.now(UTC).strftime(DT_FMT)
if not (ev["signup_open_at"] <= now <= ev["signup_close_at"]):
    ...  # "signup window is closed"
```
(All three are the pinned `YYYY-MM-DD HH:MM` UTC format, so lexicographic compare is valid.) Then show `_SignupView`: a multi-select whose options come from `kvkdb.get_active_types(self.conn, event_id)` (**active types only**, max 3). On selection open a modal with one speedup input per chosen type; on submit, `parse_speedups(value)` per type (ValueError → ephemeral error, write nothing), then `kvkdb.upsert_signup(self.conn, event_id, fid, ptype, minutes, interaction.user.id, datetime.now(UTC).strftime(DT_FMT))`.

- [ ] **Step 3: `/kvk_edit_signup event_id:int fid:int`** — identical flow, Global-Admin gated, `submitted_by = interaction.user.id`, target `fid` from the parameter.

- [ ] **Step 4: Manual verification** — registered player signs up for two active types (`7d`, `70h`) → two rows with correct minutes; re-submit changes one → updated not duplicated; inactive type not offered; unregistered → register prompt; after close → "closed"; `abc` → parse error, no row.

- [ ] **Step 5: Lint & commit** (`feat(kvk): self-service signup + admin edit`).

---

## Task 6: `/kvk_report` — rank, assign, editable preview (`cogs/kvk_report.py`)

**Files:** Create `cogs/kvk_report.py`.

**Interfaces:** `class KvkReport(commands.Cog)`; `_compute(conn, event_id) -> bool`; `_render_embeds(conn, event_id) -> list[discord.Embed]`.

- [ ] **Step 1: Imports (RELATIVE) + assignment driver honoring locks**

```python
"""KvK report: rank submissions, auto-assign slots, admin override, publish."""
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from . import kvk_data as kvkdb
from .kvk_util import generate_time_slots, rank_and_assign


def _alliance_of(fid: int):
    users = sqlite3.connect("db/users.sqlite")
    try:
        row = users.execute("SELECT alliance FROM users WHERE fid = ?", (fid,)).fetchone()
        return str(row[0]) if row and row[0] is not None else None  # TEXT affinity → keep str
    finally:
        users.close()


class KvkReport(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _compute(self, conn, event_id) -> bool:
        ev = kvkdb.get_event(conn, event_id)
        if not ev:
            return False
        slot_mode = ev["slot_mode"]
        full_day = len(generate_time_slots(slot_mode))
        skipped = []
        for ptype in kvkdb.get_active_types(conn, event_id):
            signups = kvkdb.get_signups(conn, event_id, ptype)
            if ev["scope"] == "kingdom":
                locks = kvkdb.get_locks(conn, event_id, ptype, 0)
                kvkdb.save_slots(conn, event_id, ptype, 0,
                                 rank_and_assign(signups, full_day, slot_mode, locked=locks))
            else:
                by_all = {}
                for s in signups:
                    aid = _alliance_of(s["fid"])
                    try:
                        aid_int = int(aid) if aid is not None else None
                    except ValueError:
                        aid_int = None
                    if aid_int is None:
                        skipped.append(s["fid"])
                        continue
                    by_all.setdefault(aid_int, []).append(s)
                n = ev["slots_per_alliance"] or 0
                for aid_int, group in by_all.items():
                    locks = kvkdb.get_locks(conn, event_id, ptype, aid_int)
                    kvkdb.save_slots(conn, event_id, ptype, aid_int,
                                     rank_and_assign(group, n, slot_mode, locked=locks))
        kvkdb.set_status(conn, event_id, "assigned")
        self._skipped = skipped  # surfaced in the preview
        return True
```

- [ ] **Step 2: `/kvk_report event_id:int`, render, override view**

Global-Admin gated (`sched = self.bot.get_cog("KvkScheduling")`; `sched._is_global_admin(interaction)`). If `_compute` returns False → ephemeral "unknown event". `_render_embeds` builds **one embed per position type** (kingdom 48/49 lines and alliance-grouped lists exceed the 4096-char/embed and 1024-char/field limits, so never put everything in one embed; page long alliance lists into ≤1024-char fields or multiple messages). Each line: `slot_time — nickname (fid) — {Xh Ym}` (speedups from `kvkdb.get_signup_minutes`), `🔒` if locked, `— empty —` for `fid is None`; append a "⚠️ Skipped (no alliance): …" line if `self._skipped`. Nicknames from `users.sqlite`. `_OverrideView` buttons: **Group select** (pick position_type + alliance) then **Swap** (two indices), **Lock/Unlock** (`set_slot(..., locked=toggle)`), **Clear** (`set_slot(..., fid=None, locked=0)`), **Re-run** (`_compute` — locks now persist via `get_locks`), **Publish** (Task 7).

- [ ] **Step 3: Manual verification** — alliance scope N=2, two alliances seeded: each alliance's top-2 fill, ties by earlier submit, empty when <2, skipped-note when a signup's fid has no alliance. Kingdom scope: 48/49 and kingdom-wide ranking. Lock a slot, Re-run → locked fid stays. Unknown event id → error.

- [ ] **Step 4: Lint & commit** (`feat(kvk): /kvk_report ranking, assignment, override`).

---

## Task 7: `/kvk_publish` (`cogs/kvk_report.py`)

**Files:** Modify `cogs/kvk_report.py`.

- [ ] **Step 1: Publish**

```python
    @app_commands.command(name="kvk_publish", description="Publish the KvK schedule (Global Admin).")
    async def kvk_publish(self, interaction: discord.Interaction, event_id: int):
        sched = self.bot.get_cog("KvkScheduling")
        if sched is None or not sched._is_global_admin(interaction):
            await interaction.response.send_message("Global Admin only.", ephemeral=True)
            return
        ev = kvkdb.get_event(sched.conn, event_id)
        if not ev or ev["status"] not in ("assigned", "published"):
            await interaction.response.send_message("Run /kvk_report first.", ephemeral=True)
            return
        if not ev["publish_channel_id"]:
            await interaction.response.send_message("No publish channel set.", ephemeral=True)
            return
        channel = self.bot.get_channel(int(ev["publish_channel_id"]))
        if channel is None:
            channel = await self.bot.fetch_channel(int(ev["publish_channel_id"]))
        await interaction.response.defer(ephemeral=True)
        for embed in self._render_embeds(sched.conn, event_id):
            await channel.send(embed=embed)
        kvkdb.set_status(sched.conn, event_id, "published")
        await interaction.followup.send("Published.", ephemeral=True)
```

- [ ] **Step 2: Manual verification** — publish before report → "Run /kvk_report first"; after report → embeds appear in channel, status `published`; deleted/uncached channel handled by `fetch_channel`.

- [ ] **Step 3: Lint & commit** (`feat(kvk): /kvk_publish`).

---

## Task 8: Register cogs + full smoke test

**Files:** Modify `main.py` (cog list at `main.py:1499`).

- [ ] **Step 1: Register** — add bare names to the loader list (it prepends `cogs.`):

```python
    "kvk_scheduling",
    "kvk_report",
```
Confirm the auto-loader (if any) skips `kvk_util.py` / `kvk_data.py` (no `setup`); `kvk_report.py` needs its own `async def setup(bot): await bot.add_cog(KvkReport(bot))`.

- [ ] **Step 2: Full local smoke test** (OCR-free venv): boot, confirm no import error and both cogs load, `/kvk_*` appear after sync; walk create → signup (2 players) → report → override → publish; verify `db/kvk.sqlite` rows in all four tables.

- [ ] **Step 3: Whole suite + lint**

```bash
python -m pytest tests/ -q
ruff check cogs/kvk_util.py cogs/kvk_data.py cogs/kvk_scheduling.py cogs/kvk_report.py tests/
```
Expected: all PASS, ruff clean.

- [ ] **Step 4: Commit** (`feat(kvk): register KvK cogs in the loader`).

---

## Notes for the implementer

- Do NOT edit `minister_*`. Only `main.py`'s cog list is touched.
- Discord UI (Tasks 4–7) can't be unit-tested here — they rely on manual verification. All testable logic lives in `kvk_util` / `kvk_data` (Tasks 1–3, full pytest). Keep it that way: no ranking/SQL logic inside the cogs.
- After every task the branch stays green: `python -m pytest tests/ -q` and `ruff check` on changed files.
- Spec deltas ratified by validation: tests use `pytest` under `tests/` (supersedes spec §12's `kvk_selfcheck.py`); event `status` starts at `'collecting'` (spec §4's `'draft'` state is unused).

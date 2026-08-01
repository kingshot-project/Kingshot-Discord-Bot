# KvK Scheduling — Design Spec

- **Date:** 2026-08-01
- **Status:** Approved (design), pending spec review
- **Component:** Kingshot Discord Bot — new KvK (Kingdom vs Kingdom) minister-slot planner
- **Approach:** Standalone subsystem, isolated from the existing `minister_*` cogs

## 1. Purpose

During KvK (Kingdom of Power) prep, each buff type runs a rotation of minister
positions in 30-minute slots. The player who holds the slot spends speedups to
generate points. This subsystem lets a kingdom/alliance:

1. Create a KvK event (date, scope, active position types, data-collection window).
2. Collect self-service submissions from players: which position(s) they want and
   how many speedups they will spend on each.
3. Rank players by speedups and auto-assign the top players into the 30-minute slots,
   with admin override, then publish the schedule.

## 2. Domain decisions

- **Position types with slots: 3** — `Training`, `Research`, `Building`.
- **General is NOT a position.** "General" speedups are universal; the player folds
  them into the number they submit for whichever position they will use them on.
- **Priority = speedups.** More speedup-minutes = higher priority, ranked per position type.
- **A player may submit 1-3 positions** (each type's rotation is a different window/day,
  so one player can win slots in more than one type).

## 3. Scope

**In scope:** event creation, self-service player submissions, admin edits, ranking,
auto-assignment into 30-min slots, admin override, publishing to one channel, a
speedup-string parser, a standalone verification script.

**Out of scope (this spec):** cross-alliance de-confliction of identical slot times in
alliance scope (each alliance plans its own rotation); reminders/pings before a slot;
integration with the existing `minister_*` boards; migrating `minister_*` to the shared
slot util (optional later cleanup).

## 4. Data model — new file `db/kvk.sqlite` (isolation)

```
kvk_events(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    name          TEXT NOT NULL,
    event_date    TEXT NOT NULL,          -- KvK reference date (ISO), used to time the report
    scope         TEXT NOT NULL,          -- 'alliance' | 'kingdom'
    slots_per_alliance INTEGER,           -- N; used only when scope='alliance'
    slot_mode     INTEGER NOT NULL DEFAULT 0,  -- 0 = grid from 00:00 (48/day), 1 = offset from 00:15 (49/day)
    signup_open_at  TEXT NOT NULL,        -- ISO
    signup_close_at TEXT NOT NULL,        -- ISO
    publish_channel_id INTEGER,           -- one common KvK channel
    status        TEXT NOT NULL DEFAULT 'draft',  -- draft -> collecting -> assigned -> published
    created_by    INTEGER NOT NULL,
    created_at    TEXT NOT NULL
)

kvk_event_types(
    event_id      INTEGER NOT NULL,
    position_type TEXT NOT NULL,          -- 'Training' | 'Research' | 'Building'
    type_date     TEXT NOT NULL,          -- the calendar day this type's rotation runs (ISO date)
    PRIMARY KEY (event_id, position_type)
)

kvk_signups(
    event_id      INTEGER NOT NULL,
    fid           INTEGER NOT NULL,
    position_type TEXT NOT NULL,
    speedup_minutes INTEGER NOT NULL,     -- parsed from free-form time input
    submitted_by  INTEGER NOT NULL,       -- discord id who entered it (player or admin)
    submitted_at  TEXT NOT NULL,          -- ISO; also the tie-break key
    PRIMARY KEY (event_id, fid, position_type)
)

kvk_slots(
    event_id      INTEGER NOT NULL,
    position_type TEXT NOT NULL,
    alliance_id   INTEGER NOT NULL DEFAULT 0,  -- alliance owning the slot; 0 for kingdom scope
                                          -- (NOT NULL: SQLite lets NULLs into composite PKs, breaking uniqueness)
    slot_index    INTEGER NOT NULL,       -- 0-based index into the day grid
    slot_time     TEXT NOT NULL,          -- 'HH:MM' from get_time_slots(slot_mode)
    fid           INTEGER,                -- assigned player; NULL = empty slot
    locked        INTEGER NOT NULL DEFAULT 0,  -- 1 = admin locked, auto-assign must not move it
    PRIMARY KEY (event_id, position_type, alliance_id, slot_index)
)
```

`fid -> alliance / nickname` is resolved from `users.sqlite` at report time. The Discord
user -> fid link reuses the existing `/register` flow (a player must be registered to submit).

Schema is additive-only, WAL, opened per the project's existing connection pattern.

## 5. Commands & flow

| Command | Actor | Purpose |
|---|---|---|
| `/kvk_create` | Global Admin | Wizard: name, event_date, scope, N (alliance only), slot_mode, active types + each `type_date`, signup window, publish channel. Sets status `collecting`, posts an announcement. |
| `/kvk_signup` | any registered player | Within the signup window: pick 1-3 positions; for each, enter speedups as free-form time. Idempotent upsert of that player's rows. |
| `/kvk_edit_signup` | admin | Edit/add a submission on behalf of a player. |
| `/kvk_report` | Global Admin | Rank + auto-assign top players into slots, show an editable preview (swap / lock / clear slot), status `assigned`. |
| `/kvk_publish` | Global Admin | Post the final schedule to the KvK channel, status `published`. |

The report can be produced a day before `event_date` or on demand via the command/button.

## 6. Ranking & auto-assignment algorithm

For each **scope-unit x position_type**:

1. Collect `kvk_signups` rows for that type (and, in alliance scope, that alliance —
   the alliance is derived from `users.alliance`; note it has TEXT affinity, so
   compare/group alliance ids as strings).
2. Sort by `speedup_minutes` DESC; tie-break by earlier `submitted_at`, then by `fid`.
3. Determine slot count:
   - **alliance scope:** `N = slots_per_alliance` slots for each alliance.
   - **kingdom scope:** the full day — `48` slots (slot_mode 0) or `49` slots (slot_mode 1).
4. Assign the sorted players to slot indices `0..count-1`; players beyond the count get no slot.
5. Empty slots remain empty when submissions < slot count.
6. `slot_time = get_time_slots(slot_mode)[slot_index]`.
7. Locked slots (`locked=1`) are preserved: their `fid` is fixed and the assigned player is
   removed from the ranked pool before filling the remaining unlocked slots.

A player may hold a slot in more than one type (different rotation days).

Admin override in the preview: swap two slots, lock a slot, or clear a slot; re-running
auto-assign respects locks.

## 7. Slot / time model

Slot times come from a single shared pure function `generate_time_slots(slot_mode)`
in a new module `cogs/kvk_util.py`, identical to the minister logic:

- **Mode 0 (standard):** `00:00, 00:30, ... 23:30` -> **48** slots (each 30 min).
- **Mode 1 (offset):** two 15-min edge slots (`00:00-00:15`, `23:45-00:00`) plus 47 full
  30-min slots -> **49** slots. Total still 24h.

KvK imports this function. The existing `MinisterSchedule.get_time_slots` may later be
migrated to the shared module (pure function, low risk) but that is not required for KvK
and is out of scope here to keep the minister cog untouched.

Slots map onto the type's `type_date`. Alliance scope: each alliance's N slots take grid
indices `0..N-1` from the start of the day (cross-alliance time de-confliction is a human
concern, out of scope). Kingdom scope: one rotation of 48/49 slots covering the whole day.

## 8. Speedup parsing

Pure function `parse_speedups(text) -> int (minutes)` in `cogs/kvk_util.py`:

- Accepts free-form combined time: `"7d 12h"`, `"70h"`, `"3d"`, `"90m"`, `"2d 4h 30m"`.
- Units: `d` (day=1440m), `h` (hour=60m), `m` (minute=1m). Case-insensitive, spaces optional.
- Sums all matched unit tokens; returns total minutes.
- Invalid / no recognizable token -> raises `ValueError` (caller shows a friendly message).

## 9. Permissions

- `/kvk_create`, `/kvk_report`, `/kvk_publish`, `/kvk_edit_signup`: Global Admin.
- `/kvk_signup`: any registered player (must have an `/register` fid link).

## 10. Isolation & file structure

- `cogs/kvk_scheduling.py` — event lifecycle, wizard, self-service signup, storage.
- `cogs/kvk_report.py` — ranking, auto-assignment, editable preview, publish.
- `cogs/kvk_util.py` — pure helpers: `generate_time_slots(slot_mode)`, `parse_speedups(text)`.

Each cog kept under ~600 lines to avoid the `minister_menu.py` (1864-line) trap. No edits
to `minister_*` are required.

## 11. Error handling & edge cases

- Signup outside the window -> reject with a message; closed events reject signups.
- Unregistered player runs `/kvk_signup` -> prompt to `/register` first.
- Player picks more than 3 positions -> reject at input.
- `parse_speedups` failure -> ephemeral error, no row written.
- `/kvk_report` on an event with zero signups for a type -> that type shows all-empty slots.
- Alliance scope with a player whose alliance is missing/deleted -> excluded with a note.
- All `fetchone()` results guarded before indexing; all connections closed via try/finally.
- Deleting/duplicate submissions: upsert on PK `(event_id, fid, position_type)`.

## 12. Testing

No test suite exists in the repo. A standalone script `scripts/kvk_selfcheck.py` verifies
the two deterministic cores:

1. **`parse_speedups`** — unit combinations, ordering, case, spaces, invalid input,
   boundary values.
2. **auto-assignment** — ranking order, tie-breaks (speedups equal -> earlier submit ->
   lower fid), N-cap, kingdom 48/49 counts, empty slots when submissions < count, locked-slot
   preservation.

## 13. Future / not now

- Reminders/pings before a player's slot.
- Cross-alliance slot de-confliction in alliance scope.
- Migrate `minister_*` to the shared `generate_time_slots`.
- Historical archive of past KvK schedules.

"""Read-only access to the bot's alliance roster and player->alliance binding.

The KvK scheduler needs to know which alliances exist (to add them to an event) and which alliance a
player is bound to (to gate signups and group the schedule). Both live in the main bot's databases,
not in db/kvk.sqlite, so this module reads them directly. Functions are module-level so tests and
smokes can monkeypatch them the same way they patch kvk_report._nicknames_for.
"""
import sqlite3

_ALLIANCE_DB = "db/alliance.sqlite"
_USERS_DB = "db/users.sqlite"


def list_alliances() -> list[tuple[int, str]]:
    """Every alliance the bot knows, as (alliance_id, name) ordered by id."""
    con = sqlite3.connect(_ALLIANCE_DB)
    try:
        rows = con.execute("SELECT alliance_id, name FROM alliance_list ORDER BY alliance_id").fetchall()
    finally:
        con.close()
    return [(int(aid), name) for aid, name in rows]


def alliance_id_of(fid: int):
    """The int alliance id a player is bound to, or None if unbound or unknown (users.alliance is TEXT)."""
    con = sqlite3.connect(_USERS_DB)
    try:
        row = con.execute("SELECT alliance FROM users WHERE fid = ?", (fid,)).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None

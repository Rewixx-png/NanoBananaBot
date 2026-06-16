"""
NanoHatani own Gemini key pool.
Syncs live keys from keyhunter DB into local nano_keys.db,
tracks per-key cooldowns independently from keyhunter.
"""
import sqlite3
import time
import logging
import random

from keys_manager import REWTEST_DB

NANO_KEYS_DB = "/root/Projects/NanoHatani/nano_keys.db"
_COOLDOWN_429 = 65.0
_COOLDOWN_403 = 300.0

logger = logging.getLogger(__name__)


def init_db():
    con = sqlite3.connect(NANO_KEYS_DB, timeout=5)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gemini_keys (
            key             TEXT PRIMARY KEY,
            cooldown_until  REAL DEFAULT 0,
            added_at        REAL DEFAULT 0
        )
    """)
    con.commit()
    con.close()


def sync_from_keyhunter() -> int:
    """Copy live gemini-3.5-flash keys from keyhunter DB. Returns count of keys after sync."""
    try:
        src = sqlite3.connect(REWTEST_DB, timeout=3)
        rows = src.execute(
            "SELECT key FROM keys "
            "WHERE service='Gemini' AND is_live=1 "
            "AND info NOT LIKE '%quota exhausted%'"
        ).fetchall()
        src.close()
    except Exception as e:
        logger.warning(f"nano_keys sync: keyhunter read failed: {e}")
        return 0

    if not rows:
        return 0

    now = time.time()
    con = sqlite3.connect(NANO_KEYS_DB, timeout=5)
    con.executemany(
        "INSERT OR IGNORE INTO gemini_keys (key, added_at) VALUES (?, ?)",
        [(r[0], now) for r in rows],
    )
    # Remove keys no longer live in keyhunter
    kh_keys = {r[0] for r in rows}
    existing = {r[0] for r in con.execute("SELECT key FROM gemini_keys").fetchall()}
    stale = existing - kh_keys
    if stale:
        con.executemany("DELETE FROM gemini_keys WHERE key=?", [(k,) for k in stale])
    con.commit()
    count = con.execute("SELECT COUNT(*) FROM gemini_keys").fetchone()[0]
    con.close()
    logger.info(f"nano_keys sync: {count} keys in pool ({len(stale)} removed)")
    return count


def get_live_keys() -> list[str]:
    """Return shuffled list of keys not currently in cooldown."""
    try:
        con = sqlite3.connect(NANO_KEYS_DB, timeout=3)
        now = time.time()
        rows = con.execute(
            "SELECT key FROM gemini_keys WHERE cooldown_until < ?", (now,)
        ).fetchall()
        con.close()
        keys = [r[0] for r in rows]
        random.shuffle(keys)
        return keys
    except Exception as e:
        logger.warning(f"nano_keys get_live_keys: {e}")
        return []


def mark_cooldown(key: str, status_code: int):
    """Put key into cooldown after 429 or 403."""
    seconds = _COOLDOWN_403 if status_code == 403 else _COOLDOWN_429
    until = time.time() + seconds
    try:
        con = sqlite3.connect(NANO_KEYS_DB, timeout=3)
        con.execute("UPDATE gemini_keys SET cooldown_until=? WHERE key=?", (until, key))
        con.commit()
        con.close()
    except Exception as e:
        logger.warning(f"nano_keys mark_cooldown: {e}")


def live_count() -> int:
    try:
        con = sqlite3.connect(NANO_KEYS_DB, timeout=3)
        n = con.execute(
            "SELECT COUNT(*) FROM gemini_keys WHERE cooldown_until < ?", (time.time(),)
        ).fetchone()[0]
        con.close()
        return n
    except Exception:
        return 0

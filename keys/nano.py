"""
NanoHatani own Gemini key pool.
Syncs live keys from keyhunter DB into local nano_keys.db,
tracks per-key cooldowns independently from keyhunter.
"""
import aiosqlite
import time
import logging
import random

from config import KEYHUNTER_DB, NANO_KEYS_DB, GEMINI_KEY_COOLDOWN_429, GEMINI_KEY_COOLDOWN_403

logger = logging.getLogger(__name__)


async def init_db():
    async with aiosqlite.connect(NANO_KEYS_DB, timeout=5) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gemini_keys (
                key             TEXT PRIMARY KEY,
                cooldown_until  REAL DEFAULT 0,
                added_at        REAL DEFAULT 0
            )
        """)
        await db.commit()


async def sync_from_keyhunter() -> int:
    """Copy live gemini-3.5-flash keys from keyhunter DB. Returns count of keys after sync."""
    try:
        async with aiosqlite.connect(KEYHUNTER_DB, timeout=3) as src:
            async with src.execute(
                "SELECT key FROM keys "
                "WHERE service='Gemini' AND is_live=1 "
                "AND info NOT LIKE '%quota exhausted%'"
            ) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        logger.warning(f"nano_keys sync: keyhunter read failed: {e}")
        return 0

    if not rows:
        return 0

    now = time.time()
    try:
        async with aiosqlite.connect(NANO_KEYS_DB, timeout=5) as db:
            await db.executemany(
                "INSERT OR IGNORE INTO gemini_keys (key, added_at) VALUES (?, ?)",
                [(r[0], now) for r in rows],
            )
            # Remove keys no longer live in keyhunter
            kh_keys = {r[0] for r in rows}
            async with db.execute("SELECT key FROM gemini_keys") as cur_exist:
                existing_rows = await cur_exist.fetchall()
                existing = {r[0] for r in existing_rows}
            stale = existing - kh_keys
            if stale:
                await db.executemany("DELETE FROM gemini_keys WHERE key=?", [(k,) for k in stale])
            await db.commit()
            async with db.execute("SELECT COUNT(*) FROM gemini_keys") as cur_count:
                count = (await cur_count.fetchone())[0]
            logger.info(f"nano_keys sync: {count} keys in pool ({len(stale)} removed)")
            return count
    except Exception as e:
        logger.warning(f"nano_keys sync: local pool write failed: {e}")
        return 0


async def get_live_keys() -> list[str]:
    """Return shuffled list of keys not currently in cooldown."""
    try:
        async with aiosqlite.connect(NANO_KEYS_DB, timeout=3) as db:
            now = time.time()
            async with db.execute(
                "SELECT key FROM gemini_keys WHERE cooldown_until < ?", (now,)
            ) as cur:
                rows = await cur.fetchall()
            keys = [r[0] for r in rows]
            random.shuffle(keys)
            return keys
    except Exception as e:
        logger.warning(f"nano_keys get_live_keys: {e}")
        return []


async def mark_cooldown(key: str, status_code: int):
    """Put key into cooldown after 429 or 403."""
    seconds = GEMINI_KEY_COOLDOWN_403 if status_code == 403 else GEMINI_KEY_COOLDOWN_429
    until = time.time() + seconds
    try:
        async with aiosqlite.connect(NANO_KEYS_DB, timeout=3) as db:
            await db.execute("UPDATE gemini_keys SET cooldown_until=? WHERE key=?", (until, key))
            await db.commit()
    except Exception as e:
        logger.warning(f"nano_keys mark_cooldown: {e}")



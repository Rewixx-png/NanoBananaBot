"""Live API keys — KeyHunter SQLite DB is the single source of truth.

The keyhunter service (master + workers) validates keys and writes them into the
`keys` table; every loader here reads that table. There is no key file: no
r.txt, no r.txt.enc, no encryption password. Environment variables are only an
optional override for the few services whose ops keys are not scraped.
"""
import logging
import os
import re
import time

import aiosqlite

from config import KEYHUNTER_DB

_session_dead: dict[str, float] = {}  # key -> expiry timestamp
_missing_db_warned = False


def _is_dead(key: str) -> bool:
    """Return True if key is in cooldown and hasn't expired yet."""
    exp = _session_dead.get(key)
    if exp is None:
        return False
    if time.time() < exp:
        return True
    del _session_dead[key]  # expired - prune
    return False


def strip_code_fences(content):
    content = content.strip()
    if content.startswith('```json'):
        content = content[7:]
    elif content.startswith('```'):
        content = content[3:]
    if content.endswith('```'):
        content = content[:-3]
    return content.strip()


def normalize_key_list(value):
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.splitlines()
    else:
        return []
    keys = []
    seen = set()
    for item in raw_items:
        for piece in re.split('[\\s,]+', str(item).strip()):
            key = piece.strip().strip('"\'')
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    return keys


async def _live_rows(service: str, where: str = '1=1', params: tuple = (), order: str = '') -> list[tuple[str, str]]:
    """(key, info) for a live service in KeyHunter, minus keys cooled down this session."""
    global _missing_db_warned
    if not os.path.exists(KEYHUNTER_DB):
        if not _missing_db_warned:
            logging.error(
                'KeyHunter DB not found at %s — no live keys for any service. '
                'Deploy keyhunter and point KEYHUNTER_DB at its keyhunter.db.', KEYHUNTER_DB
            )
            _missing_db_warned = True
        return []
    sql = f"SELECT key, info FROM keys WHERE service=? AND is_live=1 AND ({where})"
    if order:
        sql += f' ORDER BY {order}'
    try:
        async with aiosqlite.connect(KEYHUNTER_DB, timeout=3) as db:
            async with db.execute(sql, (service, *params)) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        logging.warning(f'KeyHunter query failed for {service}: {type(e).__name__}: {e}')
        return []
    return [(row[0], row[1] or '') for row in rows if not _is_dead(row[0])]


def _env_keys(*names: str) -> list[str]:
    keys: list[str] = []
    for name in names:
        keys += normalize_key_list(os.getenv(name, ''))
    return list(dict.fromkeys(k for k in keys if not _is_dead(k)))


async def load_keys(model_filter: str = None):
    """Load live Gemini keys, optionally filtered by model access from info field."""
    if model_filter:
        rows = await _live_rows('Gemini', 'info LIKE ?', (f'%{model_filter}%',))
        if rows:
            return [row[0] for row in rows]
    rows = await _live_rows('Gemini')
    if rows:
        return [row[0] for row in rows]
    return await _premium_gemini_keys(model_filter)


async def _premium_gemini_keys(model_filter: str = None) -> list[str]:
    """Reserved/sold Gemini keys — last resort when nothing is live."""
    if not os.path.exists(KEYHUNTER_DB):
        return []
    where = "service='Gemini' AND status IN ('reserved', 'sold', 'rate_limited')"
    params: tuple = ()
    if model_filter:
        where += " AND deep_check LIKE ?"
        params = (f'%{model_filter}%',)
    sql = (
        f"SELECT key FROM premium_keys WHERE {where} "
        "ORDER BY CASE status WHEN 'reserved' THEN 0 WHEN 'sold' THEN 1 ELSE 2 END, last_validated_at DESC"
    )
    try:
        async with aiosqlite.connect(KEYHUNTER_DB, timeout=3) as db:
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        logging.warning(f'premium_keys query failed: {type(e).__name__}: {e}')
        return []
    return [row[0] for row in rows if not _is_dead(row[0])]


async def load_openai_keys():
    rows = await _live_rows(
        'OpenAI',
        "info NOT LIKE '%quota%' AND info NOT LIKE '%QUOTA%'",
        order="CASE WHEN info LIKE '%flagship%' THEN 0 ELSE 1 END",
    )
    keys = [key for key, info in rows if not info.startswith('⚠️')]
    if keys:
        return keys
    return _env_keys('OPENAI_API_KEY')


async def load_nvidia_keys():
    rows = await _live_rows('Nvidia')
    keys = [row[0] for row in rows]
    return keys or _env_keys('NVIDIA_API_KEY')


async def load_openrouter_keys():
    # A gateway key (OmniRoute or a personal openrouter.ai key) wins when
    # configured, then the scraped pool.
    from config import OPENROUTER_API_KEY
    if OPENROUTER_API_KEY and not _is_dead(OPENROUTER_API_KEY):
        return [OPENROUTER_API_KEY]
    rows = await _live_rows('OpenRouter')
    return [row[0] for row in rows]


async def load_replicate_keys():
    rows = await _live_rows('Replicate')
    return [row[0] for row in rows]


async def load_groq_keys():
    rows = await _live_rows('Groq')
    keys = [row[0] for row in rows]
    return keys or _env_keys('GROQ_API_KEY')


async def load_firecrawl_keys():
    """Load live Firecrawl keys, highest remaining credits first."""
    rows = await _live_rows('Firecrawl')

    def _credits(info: str) -> int:
        # info looks like "Firecrawl LIVE · 85585/100000 credits"
        m = re.search(r'(\d+)/\d+\s*credits', info)
        return int(m.group(1)) if m else 0

    ordered = [key for key, _ in sorted(rows, key=lambda row: _credits(row[1]), reverse=True)]
    return list(dict.fromkeys([*ordered, *_env_keys('FIRECRAWL_API_KEY', 'FIRECRAWL_KEYS')]))


async def load_suno_keys():
    """Live Suno (sunoapi.org) keys, richest wallet first.

    One V6 generation costs ~12 credits, so ordering by balance matters: most
    scraped keys hold 2-10 credits and can only serve as a fallback.
    """
    rows = await _live_rows('Suno')

    def _credits(info: str) -> float:
        m = re.search(r'([\d.]+)\s*credits', info)
        return float(m.group(1)) if m else 0.0

    return [key for key, _ in sorted(rows, key=lambda row: _credits(row[1]), reverse=True)]


async def _retire_in_db(key: str) -> None:
    """Flip is_live=0 so the pool stops handing out a key that came back 401/402."""
    if not os.path.exists(KEYHUNTER_DB):
        return
    try:
        async with aiosqlite.connect(KEYHUNTER_DB, timeout=3) as db:
            await db.execute("UPDATE keys SET is_live=0 WHERE key=? AND is_live=1", (key,))
            await db.commit()
        logging.info(f"Ключ {key[:10]}... помечен мёртвым в KeyHunter (401/402/400).")
    except Exception as e:
        logging.warning(f'Не смог пометить ключ мёртвым в KeyHunter: {type(e).__name__}: {e}')


async def remove_key(key_to_remove, status_code=None):
    """Cool a key down in-process; a key that is permanently dead is also retired in KeyHunter."""
    if status_code == 429:
        _session_dead[key_to_remove] = time.time() + 65
        logging.info(f"Ключ {key_to_remove[:10]}... в кулдауне 65с (429 rate limit).")
        return
    if status_code == 403:
        _session_dead[key_to_remove] = time.time() + 300
        logging.info(f"Ключ {key_to_remove[:10]}... в кулдауне 300с (403 forbidden).")
        return
    _session_dead[key_to_remove] = time.time() + 86400 * 365
    await _retire_in_db(key_to_remove)

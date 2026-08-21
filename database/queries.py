"""
Consolidated SQLite database queries for NanoHatani.
Replaces fragmented micro-modules (history, limits, tasks, users, generations, voice).
"""
import json
import logging
import os
import time
from datetime import date
from typing import List, Dict, Any, Optional

from database.connection import get_db

logger = logging.getLogger(__name__)

# ── Fernet encryption for veo_api_key ─────────────────────────────────────
_fernet = None

def _get_fernet():
    """Load or create a Fernet instance from DB_ENCRYPTION_KEY env var."""
    global _fernet
    if _fernet is not None:
        return _fernet
    from cryptography.fernet import Fernet
    key = os.getenv('DB_ENCRYPTION_KEY', '').strip()
    if key:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    else:
        logger.warning('DB_ENCRYPTION_KEY not set in .env — using a temporary key. Veo API keys down to temporary memory.')
        _fernet = Fernet(Fernet.generate_key())
    return _fernet

def _encrypt_key(plain: Optional[str]) -> Optional[str]:
    if not plain:
        return plain
    try:
        return _get_fernet().encrypt(plain.encode()).decode()
    except Exception as e:
        logger.error(f'Failed to encrypt veo_api_key: {e}')
        return None

def _decrypt_key(encrypted: Optional[str]) -> Optional[str]:
    if not encrypted:
        return encrypted
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.error(f'Failed to decrypt veo_api_key: {e}')
        return None

# ── Chat history ──────────────────────────────────────────────────────────
async def get_history(chat_id: int) -> List[Dict[str, Any]]:
    try:
        async with get_db() as db:
            async with db.execute('SELECT history FROM chat_history WHERE chat_id = ?', (chat_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return json.loads(row[0])
                return []
    except json.JSONDecodeError as e:
        logger.error(f'Ошибка декодирования JSON для chat_id {chat_id}: {e}')
        return []
    except Exception as e:
        logger.exception(f'Ошибка при получении истории для chat_id {chat_id}: {e}')
        return []

async def save_history(chat_id: int, history: List[Dict[str, Any]]):
    try:
        async with get_db() as db:
            await db.execute(
                'INSERT OR REPLACE INTO chat_history (chat_id, history) VALUES (?, ?)',
                (chat_id, json.dumps(history, ensure_ascii=False))
            )
            await db.commit()
    except Exception as e:
        logger.exception(f'Ошибка при сохранении истории для chat_id {chat_id}: {e}')
        raise

# ── Pending generations ───────────────────────────────────────────────────
async def save_pending_gen(
    gen_id: str, gen_type: str, user_id: int, chat_id: int,
    source_message_id: int, message_thread_id: Optional[int],
    prompt: str, model: str, provider: str,
    file_ids: list = None, veo_operation_name: str = None,
    veo_api_key: str = None, model_label: str = ''
):
    try:
        async with get_db() as db:
            await db.execute('''
                INSERT OR REPLACE INTO pending_generations
                (id, gen_type, user_id, chat_id, source_message_id, message_thread_id,
                 prompt, model, provider, file_ids, veo_operation_name, veo_api_key, model_label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                gen_id, gen_type, user_id, chat_id, source_message_id, message_thread_id,
                prompt, model, provider, json.dumps(file_ids or []),
                veo_operation_name, _encrypt_key(veo_api_key), model_label, time.time()
            ))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка сохранения pending_gen {gen_id}: {e}')

async def delete_pending_gen(gen_id: str):
    try:
        async with get_db() as db:
            await db.execute('DELETE FROM pending_generations WHERE id = ?', (gen_id,))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка удаления pending_gen {gen_id}: {e}')

async def get_all_pending_gens() -> List[Dict[str, Any]]:
    try:
        async with get_db() as db:
            async with db.execute('SELECT * FROM pending_generations ORDER BY created_at') as cursor:
                rows = await cursor.fetchall()
                cols = [
                    'id', 'gen_type', 'user_id', 'chat_id', 'source_message_id',
                    'message_thread_id', 'prompt', 'model', 'provider', 'file_ids',
                    'veo_operation_name', 'veo_api_key', 'model_label', 'created_at'
                ]
                result = []
                for row in rows:
                    d = dict(zip(cols, row))
                    d['file_ids'] = json.loads(d.get('file_ids') or '[]')
                    d['veo_api_key'] = _decrypt_key(d.get('veo_api_key'))
                    result.append(d)
                return result
    except Exception as e:
        logger.error(f'Ошибка загрузки pending_gens: {e}')
        return []

# ── User stats & bans ─────────────────────────────────────────────────────
async def add_user_stat(user_id: int, username: str, first_name: str, gen_type: str):
    date_str = str(date.today())
    username = username or ''
    first_name = first_name or 'Аноним'
    try:
        async with get_db() as db:
            await db.execute('''
                INSERT INTO user_stats (user_id, username, first_name, date_str, gen_type, count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id, date_str, gen_type) DO UPDATE SET
                count = count + 1, username=excluded.username, first_name=excluded.first_name
            ''', (user_id, username, first_name, date_str, gen_type))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка сохранения статистики: {e}')

async def get_user_stats(date_str: str = None) -> List[dict]:
    try:
        async with get_db() as db:
            if date_str:
                async with db.execute('''
                    SELECT user_id, username, first_name, SUM(count) as c, gen_type
                    FROM user_stats WHERE date_str = ?
                    GROUP BY user_id, username, first_name, gen_type ORDER BY c DESC
                ''', (date_str,)) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute('''
                    SELECT user_id, username, first_name, SUM(count) as c, gen_type
                    FROM user_stats
                    GROUP BY user_id, username, first_name, gen_type ORDER BY c DESC
                ''') as cur:
                    rows = await cur.fetchall()
            return [{'user_id': r[0], 'username': r[1], 'first_name': r[2], 'count': r[3], 'type': r[4]} for r in rows]
    except Exception as e:
        logger.error(f'Ошибка чтения статистики: {e}')
        return []

async def get_banned_users_db() -> set:
    try:
        async with get_db() as db:
            async with db.execute('SELECT user_id FROM banned_users') as cur:
                rows = await cur.fetchall()
                return {r[0] for r in rows}
    except Exception as e:
        logger.warning(f'Ошибка получения забаненных пользователей: {e}')
        return set()

async def add_banned_user_db(user_id: int):
    try:
        async with get_db() as db:
            await db.execute('INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)', (user_id,))
            await db.commit()
    except Exception as e:
        logger.warning(f'Ошибка добавления в бан {user_id}: {e}')

async def remove_banned_user_db(user_id: int):
    try:
        async with get_db() as db:
            await db.execute('DELETE FROM banned_users WHERE user_id = ?', (user_id,))
            await db.commit()
    except Exception as e:
        logger.warning(f'Ошибка удаления из бана {user_id}: {e}')

async def get_all_vip_users() -> dict:
    try:
        async with get_db() as db:
            async with db.execute('SELECT user_id, paid_until FROM vip_users') as cur:
                rows = await cur.fetchall()
                return {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.error(f'Ошибка при получении VIP-пользователей: {e}')
        return {}

# ── Limits & usage ────────────────────────────────────────────────────────
async def get_all_chat_limits() -> dict:
    try:
        async with get_db() as db:
            async with db.execute('SELECT chat_id, req_limit, days FROM chat_limits') as cur:
                rows = await cur.fetchall()
                return {r[0]: (r[1], r[2]) for r in rows}
    except Exception as e:
        logger.warning(f'Ошибка получения лимитов чатов: {e}')
        return {}

async def set_chat_limit_db(chat_id: int, req_limit: int, days: int):
    try:
        async with get_db() as db:
            await db.execute(
                'INSERT OR REPLACE INTO chat_limits (chat_id, req_limit, days) VALUES (?, ?, ?)',
                (chat_id, req_limit, days)
            )
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка сохранения лимита чата: {e}')

async def get_all_daily_limits_usage() -> dict:
    try:
        async with get_db() as db:
            async with db.execute('SELECT chat_id, user_id, period, count FROM daily_limits_usage') as cur:
                rows = await cur.fetchall()
                return {(r[0], r[1]): {'period': r[2], 'count': r[3]} for r in rows}
    except Exception as e:
        logger.error(f'Ошибка при получении лимитов использования: {e}')
        return {}

# ── Prompt logging ────────────────────────────────────────────────────────
async def log_prompt(user_id: int, username: str, first_name: str, gen_type: str, prompt: str):
    try:
        username = username or ''
        first_name = first_name or 'Аноним'
        async with get_db() as db:
            await db.execute(
                'INSERT INTO prompt_logs (user_id, username, first_name, gen_type, prompt, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, username, first_name, gen_type, prompt, time.time())
            )
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка логирования промпта: {e}')

async def get_recent_prompts(limit: int = 50, user_id: int = None) -> List[Dict[str, Any]]:
    try:
        async with get_db() as db:
            if user_id:
                async with db.execute(
                    'SELECT user_id, username, first_name, gen_type, prompt, created_at FROM prompt_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
                    (user_id, limit)
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(
                    'SELECT user_id, username, first_name, gen_type, prompt, created_at FROM prompt_logs ORDER BY created_at DESC LIMIT ?',
                    (limit,)
                ) as cur:
                    rows = await cur.fetchall()
            return [
                {'user_id': r[0], 'username': r[1], 'first_name': r[2], 'gen_type': r[3], 'prompt': r[4], 'created_at': r[5]}
                for r in rows
            ]
    except Exception as e:
        logger.error(f'Ошибка получения промптов: {e}')
        return []

# ── Voices & settings ─────────────────────────────────────────────────────
async def add_voice(user_id: int, name: str, voice_id: str, tier: str, duration_sec: float = 0):
    try:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO voices (user_id, name, voice_id, tier, audio_duration_sec) VALUES (?, ?, ?, ?, ?)",
                (user_id, name, voice_id, tier, duration_sec),
            )
            await db.commit()
    except Exception as e:
        logger.error(f"add_voice failed: {e}")
        raise RuntimeError(f"add_voice failed: {e}") from e

async def get_voices(user_id: int) -> list[dict]:
    try:
        async with get_db() as db:
            async with db.execute(
                "SELECT name, voice_id, tier, audio_duration_sec, created_at FROM voices WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"name": r[0], "voice_id": r[1], "tier": r[2], "duration_sec": r[3], "created_at": r[4]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"get_voices failed: {e}")
        return []

async def get_voice_by_id(user_id: int, voice_id: str) -> dict | None:
    try:
        async with get_db() as db:
            async with db.execute(
                "SELECT name, voice_id, tier, audio_duration_sec, created_at FROM voices WHERE user_id=? AND voice_id=?",
                (user_id, voice_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row:
            return {
                "name": row[0],
                "voice_id": row[1],
                "tier": row[2],
                "duration_sec": row[3],
                "created_at": row[4],
            }
    except Exception as e:
        logger.error(f"get_voice_by_id failed: {e}")
    return None

async def delete_voice(user_id: int, voice_id: str) -> bool:
    try:
        async with get_db() as db:
            cursor = await db.execute(
                "DELETE FROM voices WHERE user_id=? AND voice_id=?",
                (user_id, voice_id),
            )
            await db.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"delete_voice failed: {e}")
        raise RuntimeError(f"Local voice delete failed: {e}") from e

async def get_settings(user_id: int) -> dict:
    try:
        async with get_db() as db:
            async with db.execute(
                "SELECT tts_model, stability, similarity_boost, style, speed FROM voice_settings WHERE user_id=?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
    except Exception as e:
        logger.error(f"get_settings failed: {e}")
        raise RuntimeError(f"get_settings failed: {e}") from e
    if row:
        return {
            "tts_model": row[0],
            "stability": row[1],
            "similarity_boost": row[2],
            "style": row[3],
            "speed": row[4],
        }
    return {"tts_model": "eleven_v3", "stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "speed": 1.0}

async def save_settings(user_id: int, **kwargs):
    try:
        current = await get_settings(user_id)
        current.update(kwargs)
        async with get_db() as db:
            await db.execute(
                """INSERT OR REPLACE INTO voice_settings (user_id, tts_model, stability, similarity_boost, style, speed)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, current["tts_model"], current["stability"],
                 current["similarity_boost"], current["style"], current["speed"]),
            )
            await db.commit()
    except Exception as e:
        logger.error(f"save_settings failed: {e}")
        raise RuntimeError(f"save_settings failed: {e}") from e

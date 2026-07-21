"""
Voice cloning & TTS settings persistence for ElevenLabs integration.
"""
import logging

from database.connection import get_db

logger = logging.getLogger(__name__)


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

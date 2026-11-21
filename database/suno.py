"""SQLite persistence for Suno tracks, personas, custom voices, and stem splits."""

import json
import logging
from typing import Optional

from database.connection import get_db

logger = logging.getLogger(__name__)


async def save_suno_track(
    user_id: int,
    chat_id: int,
    task_id: str,
    audio_id: str,
    *,
    title: str = '',
    tags: str = '',
    duration: float = 0.0,
    model: str = 'V6',
    prompt: str = '',
    audio_url: str = '',
) -> Optional[int]:
    if not task_id or not audio_id:
        return None
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO suno_tracks
                (user_id, chat_id, task_id, audio_id, title, tags, duration, model, prompt, audio_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, task_id, audio_id) DO UPDATE SET
                title = excluded.title,
                tags = excluded.tags,
                duration = excluded.duration,
                model = excluded.model,
                prompt = excluded.prompt,
                audio_url = excluded.audio_url
            """,
            (user_id, chat_id, task_id, audio_id, title[:120], tags[:500],
             float(duration or 0), model[:32], prompt[:5000], audio_url[:1000]),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM suno_tracks WHERE user_id = ? AND task_id = ? AND audio_id = ?",
            (user_id, task_id, audio_id),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def get_suno_tracks(user_id: int, limit: int = 30, offset: int = 0) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, user_id, chat_id, task_id, audio_id, title, tags, duration, model, prompt, audio_url, created_at
            FROM suno_tracks
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    'id': r[0], 'user_id': r[1], 'chat_id': r[2],
                    'task_id': r[3], 'audio_id': r[4], 'title': r[5],
                    'tags': r[6], 'duration': r[7], 'model': r[8],
                    'prompt': r[9], 'audio_url': r[10], 'created_at': r[11],
                }
                for r in rows
            ]


async def get_suno_track_by_id(track_db_id: int, user_id: Optional[int] = None) -> Optional[dict]:
    async with get_db() as db:
        if user_id is not None:
            sql = """
                SELECT id, user_id, chat_id, task_id, audio_id, title, tags, duration, model, prompt, audio_url, created_at
                FROM suno_tracks WHERE id = ? AND user_id = ?
            """
            params = (track_db_id, user_id)
        else:
            sql = """
                SELECT id, user_id, chat_id, task_id, audio_id, title, tags, duration, model, prompt, audio_url, created_at
                FROM suno_tracks WHERE id = ?
            """
            params = (track_db_id,)
        async with db.execute(sql, params) as cur:
            r = await cur.fetchone()
            if not r:
                return None
            return {
                'id': r[0], 'user_id': r[1], 'chat_id': r[2],
                'task_id': r[3], 'audio_id': r[4], 'title': r[5],
                'tags': r[6], 'duration': r[7], 'model': r[8],
                'prompt': r[9], 'audio_url': r[10], 'created_at': r[11],
            }


async def delete_suno_track(track_db_id: int, user_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM suno_tracks WHERE id = ? AND user_id = ?",
            (track_db_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def save_suno_persona(
    user_id: int,
    persona_id: str,
    name: str,
    description: str = '',
    style: str = '',
    source_task_id: str = '',
    source_audio_id: str = '',
) -> Optional[int]:
    if not persona_id:
        return None
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO suno_personas
                (user_id, persona_id, name, description, style, source_task_id, source_audio_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, persona_id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                style = excluded.style
            """,
            (user_id, persona_id, name[:80], description[:500], style[:200],
             source_task_id, source_audio_id),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM suno_personas WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def get_suno_personas(user_id: int) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, user_id, persona_id, name, description, style, source_task_id, source_audio_id, created_at
            FROM suno_personas WHERE user_id = ? ORDER BY id DESC
            """,
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    'id': r[0], 'user_id': r[1], 'persona_id': r[2],
                    'name': r[3], 'description': r[4], 'style': r[5],
                    'source_task_id': r[6], 'source_audio_id': r[7], 'created_at': r[8],
                }
                for r in rows
            ]


async def delete_suno_persona(persona_db_id: int, user_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM suno_personas WHERE id = ? AND user_id = ?",
            (persona_db_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def save_suno_voice(
    user_id: int,
    task_id: str,
    voice_id: str,
    name: str,
    style: str = '',
    skill_level: str = 'beginner',
    language: str = 'ru',
) -> Optional[int]:
    if not voice_id:
        return None
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO suno_voices
                (user_id, task_id, voice_id, name, style, skill_level, language)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, voice_id) DO UPDATE SET
                task_id = excluded.task_id,
                name = excluded.name,
                style = excluded.style,
                skill_level = excluded.skill_level
            """,
            (user_id, task_id, voice_id, name[:80], style[:200], skill_level[:32], language[:16]),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM suno_voices WHERE user_id = ? AND voice_id = ?",
            (user_id, voice_id),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def get_suno_voices(user_id: int) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, user_id, task_id, voice_id, name, style, skill_level, language, created_at
            FROM suno_voices WHERE user_id = ? ORDER BY id DESC
            """,
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    'id': r[0], 'user_id': r[1], 'task_id': r[2], 'voice_id': r[3],
                    'name': r[4], 'style': r[5], 'skill_level': r[6],
                    'language': r[7], 'created_at': r[8],
                }
                for r in rows
            ]


async def get_suno_voice_by_id(voice_db_id: int, user_id: int) -> Optional[dict]:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, user_id, task_id, voice_id, name, style, skill_level, language, created_at
            FROM suno_voices WHERE id = ? AND user_id = ?
            """,
            (voice_db_id, user_id),
        ) as cur:
            r = await cur.fetchone()
            if not r:
                return None
            return {
                'id': r[0], 'user_id': r[1], 'task_id': r[2], 'voice_id': r[3],
                'name': r[4], 'style': r[5], 'skill_level': r[6],
                'language': r[7], 'created_at': r[8],
            }


async def delete_suno_voice(voice_db_id: int, user_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM suno_voices WHERE id = ? AND user_id = ?",
            (voice_db_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def save_suno_stems(
    user_id: int,
    task_id: str,
    source_title: str,
    sep_type: str,
    stems: list[dict],
) -> Optional[int]:
    if not task_id:
        return None
    payload = json.dumps(stems or [], ensure_ascii=False)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO suno_stems (user_id, task_id, source_title, sep_type, stems_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, task_id) DO UPDATE SET
                source_title = excluded.source_title,
                sep_type = excluded.sep_type,
                stems_json = excluded.stems_json
            """,
            (user_id, task_id, source_title[:120], sep_type[:40], payload),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM suno_stems WHERE user_id = ? AND task_id = ?",
            (user_id, task_id),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def get_suno_stems_list(user_id: int, limit: int = 15) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, user_id, task_id, source_title, sep_type, stems_json, created_at
            FROM suno_stems WHERE user_id = ? ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            out = []
            for r in rows:
                try:
                    stems = json.loads(r[5] or '[]')
                except Exception:
                    stems = []
                out.append({
                    'id': r[0], 'user_id': r[1], 'task_id': r[2],
                    'source_title': r[3], 'sep_type': r[4],
                    'stems': stems, 'created_at': r[6],
                })
            return out


async def get_suno_stems_by_id(stems_db_id: int, user_id: int) -> Optional[dict]:
    async with get_db() as db:
        async with db.execute(
            """
            SELECT id, user_id, task_id, source_title, sep_type, stems_json, created_at
            FROM suno_stems WHERE id = ? AND user_id = ?
            """,
            (stems_db_id, user_id),
        ) as cur:
            r = await cur.fetchone()
            if not r:
                return None
            try:
                stems = json.loads(r[5] or '[]')
            except Exception:
                stems = []
            return {
                'id': r[0], 'user_id': r[1], 'task_id': r[2],
                'source_title': r[3], 'sep_type': r[4],
                'stems': stems, 'created_at': r[6],
            }

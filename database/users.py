import logging
from datetime import date
from typing import List

from database.connection import get_db

logger = logging.getLogger(__name__)


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
    except Exception:
        return set()


async def add_banned_user_db(user_id: int):
    try:
        async with get_db() as db:
            await db.execute('INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)', (user_id,))
            await db.commit()
    except Exception:
        pass


async def remove_banned_user_db(user_id: int):
    try:
        async with get_db() as db:
            await db.execute('DELETE FROM banned_users WHERE user_id = ?', (user_id,))
            await db.commit()
    except Exception:
        pass


async def get_all_vip_users() -> dict:
    try:
        async with get_db() as db:
            async with db.execute('SELECT user_id, paid_until FROM vip_users') as cur:
                rows = await cur.fetchall()
                return {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.error(f'Ошибка при получении VIP-пользователей: {e}')
        return {}


async def set_vip_user_db(user_id: int, paid_until: float):
    try:
        async with get_db() as db:
            await db.execute('INSERT OR REPLACE INTO vip_users (user_id, paid_until) VALUES (?, ?)', (user_id, paid_until))
            await db.commit()
    except Exception as e:
        logger.error(f'Ошибка сохранения VIP-пользователя {user_id}: {e}')

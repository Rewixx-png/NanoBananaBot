import logging

from database.connection import get_db

logger = logging.getLogger(__name__)


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



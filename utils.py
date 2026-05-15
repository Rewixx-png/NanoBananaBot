import logging
from aiogram import Bot
from config import CHAT_ID, ALLOWED_USER_IDS, BANNED_USER_IDS, TEXT_ONLY_CHAT_ID, FULL_ACCESS_CHAT_ID

def is_banned(user_id: int) -> bool:
    return user_id in BANNED_USER_IDS

async def check_membership(bot: Bot, user_id: int, chat_id: int=None) -> bool:
    if user_id in BANNED_USER_IDS:
        return False
    if user_id in ALLOWED_USER_IDS:
        return True
    if chat_id in (TEXT_ONLY_CHAT_ID, FULL_ACCESS_CHAT_ID):
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHAT_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logging.error(f'Ошибка проверки подписки: {e}')
        return False
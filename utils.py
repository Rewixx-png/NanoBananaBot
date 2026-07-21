import logging
from aiogram import Bot
from config import CHAT_ID, ALLOWED_USER_IDS, BANNED_USER_IDS, TEXT_ONLY_CHAT_ID, FULL_ACCESS_CHAT_ID, ADMIN_IDS

def is_banned(user_id: int) -> bool:
    return user_id in BANNED_USER_IDS

def make_safe_caption(prefix: str, prompt: str) -> str:
    max_len = 1024
    if len(prefix) + len(prompt) <= max_len:
        return f"{prefix}{prompt}"
    allowed_prompt_len = max_len - len(prefix) - 3
    if allowed_prompt_len > 0:
        return f"{prefix}{prompt[:allowed_prompt_len]}..."
    else:
        return prefix[:max_len]

async def check_membership(bot: Bot, user_id: int, chat_id: int=None) -> bool:
    if user_id in ADMIN_IDS:
        return True
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
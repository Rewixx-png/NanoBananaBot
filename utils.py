import logging
from aiogram import Bot
from config import CHAT_ID

# ==========================================
# Проверка подписки на беседу
# ==========================================
async def check_membership(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHAT_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logging.error(f"Ошибка проверки подписки: {e}")
        # Если бот не в беседе, он не сможет проверить.
        return False

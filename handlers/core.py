import os
import asyncio
import random as _random
from aiogram import Router, types
from aiogram.filters import Command

from config import OWNER_USER_ID, CHAT_ID
from state import chat_members_cache, chat_context_buffer, chat_last_files
from database import save_history
from utils import check_membership
from ai_services import generate_bull_roast

core_router = Router()

_ALL_PHRASES = [
    'Эй вы, уроды, все сюда нахуй! 👇',
    'Хуиданте сюда все, живо! 🔔',
    'Ау, дебилы, слышите? Все сюда! 📢',
    'Все ко мне, быстро, я сказал! 🗣️',
    'Ну-ка все собрались, чего расползлись! 👊',
    'Стоять всем! Сюда смотреть! 👁️',
    'Эй ты, и ты, и ты тоже — все на месте! ⚡'
]

@core_router.message(Command('start'))
async def cmd_start(message: types.Message):
    if message.chat.type == 'private':
        is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
        if not is_member:
            await message.answer('Доступ запрещен. Вы не состоите в обязательной беседе.')
            return
        await message.answer('Привет! Доступ разрешён 🤬\n\nКоманды:\n/image ваш промпт — генерация картинки (Gemini / GPT / FLUX)\n/video ваш промпт — генерация видео через Veo\n/clear — очистить историю диалога\n\nМожно прикрепить фото к /image или /video.\nТегни меня или ответь на моё сообщение — отвечу по-плохому 🤬')

@core_router.message(Command('help'))
async def cmd_help(message: types.Message):
    text = '''🍌 Hatani AI — справка

Тегни или ответь реплаем — влезу в разговор. Пишу резко и коротко.

🤖 AI-АГЕНТ (21 инструмент)
Сам определяю когда нужен агент. Примеры:
» найди картинку Standoff 2 — ищу + проверяю через Gemini Vision
» скачай эдит от kadzu vfx — ищу, верифицирую автора, качаю
» нагрузи оперативку / покажи df -h — Docker sandbox (изолировано)
» построй график: Jan 100 Feb 150 — matplotlib → PNG
» переведи на японский: текст — Gemini перевод
» сделай QR на мой сайт — qrcode → PNG
» озвучь этот текст голосом Aoede — Gemini TTS → voice
» скачай видео [ссылка] — yt-dlp, до 720p, 48 МБ

🎨 КАРТИНКИ
/image ваш промпт — Gemini / GPT / FLUX / NSFW Replicate
Фото и альбомы прикрепляй как референс. Могу сам составить промпт по фото.
Без команды: "нарисуй кота" — сам пойму и нарисую.
Реплай на картинку + правка — отредактирую.
/up — апскейл 2x без сжатия (документом)

🎬 ВИДЕО
/video ваш промпт — Google Veo (выбор модели кнопками, до 8 сек)
Прикрепи фото — анимирую из него. Жду до 5 минут.

🎙 ОЗВУЧКА
/tts ваш текст — голос, стиль, сцена, темп, акцент через меню
Голоса: Kore, Aoede, Charon, Fenrir, Puck (есть предпрослушка)

📸 АНАЛИЗ МЕДИА
Отправь видео/GIF → покадровый анализ + аудио
Отправь голосовое → транскрипция + ответ
Отправь документ/zip → прочитаю, отвечу на вопросы, код верну файлами

🧠 ПАМЯТЬ
• Помню 100 сообщений чата (включая не адресованные мне)
• /clear — стираю историю и контекст полностью
• Понимаю реплаи и контекст разговора

🌐 ИНТЕРНЕТ
Firecrawl: 8-12 запросов → до 8 страниц каждый → 3 уровня вглубь → 16 итераций
» найди в инете... / что нового у... / поищи свежие новости...

💻 КОД И ПРОЕКТЫ
Один файл → документ. Проект → .zip с README.
Проверяю Python/JSON/HTML перед отправкой.

🎭 ПРОЧЕЕ
/bull — роаст (реплаем на юзера)
/all — тегнуть всех участников
/dual / /stopdual — два AI базарят между собой
/figma [описание] — создать дизайн через Figma Plugin

👑 АДМИНКА
/stats /prompts /limit /ban /unban /vip'''
    await message.reply(text)

async def _rebuild_sandbox_bg(message):
    import subprocess as _sp
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "build", "-f", f"{project_dir}/Dockerfile.sandbox",
            "-t", "hatani-sandbox:latest", project_dir,
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        await proc.wait()
        status = "✅ Контейнер пересобран" if proc.returncode == 0 else "❌ Ошибка пересборки"
    except Exception as e:
        status = f"❌ {e}"
    try:
        await message.answer(status)
    except Exception:
        pass

@core_router.message(Command('clear'))
async def cmd_clear(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
    await save_history(message.chat.id, [])
    chat_context_buffer.pop(message.chat.id, None)
    chat_last_files.pop(message.chat.id, None)
    await message.reply('Окей, я забыл всю хуйню, которую мы тут обсуждали. Начинаем с чистого листа.')
    if message.from_user.id == OWNER_USER_ID:
        asyncio.create_task(_rebuild_sandbox_bg(message))

@core_router.message(Command('all'))
async def cmd_all(message: types.Message):
    if message.chat.type == 'private':
        await message.reply('В личке некого созывать, дурик.')
        return
    uid = message.from_user.id
    try:
        admins = await message.bot.get_chat_administrators(message.chat.id)
        if message.chat.id not in chat_members_cache:
            chat_members_cache[message.chat.id] = {}
        for a in admins:
            u = a.user
            if not u.is_bot:
                chat_members_cache[message.chat.id][u.id] = (u.first_name or 'Аноним', u.username)
    except Exception:
        pass
    members = chat_members_cache.get(message.chat.id, {})
    if not members:
        await message.reply('Никого не знаю ещё.')
        return
    bot_user = await message.bot.get_me()
    targets = [u for u in members.keys() if u != bot_user.id]
    
    if not targets:
        await message.reply('Не на кого тегать, все и так тут.')
        return
        
    target_chunks = [targets[i:i + 5] for i in range(0, len(targets), 5)]
    
    for t_chunk in target_chunks:
        phrase = _random.choice(_ALL_PHRASES)
        mentions = [f'<a href="tg://user?id={uid}">\u200b</a>' for uid in t_chunk]
        text = f"{phrase} ({len(t_chunk)})\n" + "\u200b".join(mentions) + "\u200d"
        await message.answer(text, parse_mode='HTML')

@core_router.message(Command('bull'))
async def cmd_bull(message: types.Message):
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply('Реплаем на юзера используй, дебил.')
        return
    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.reply('На бота нельзя, придурок.')
        return
    name = target.first_name or 'Аноним'
    username = target.username or ''
    lines = await generate_bull_roast(name, username)
    reply_to_id = message.reply_to_message.message_id
    for line in lines:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=line,
            reply_to_message_id=reply_to_id,
        )
        await asyncio.sleep(0.3)

import asyncio
import random as _random
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_USER_ID
from state import chat_members_cache, chat_context_buffer, chat_last_files
from database import save_history
from utils import check_membership
from services.gemini_text import generate_bull_roast

core_router = Router()
PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="image", description="Создать картинку"),
    BotCommand(command="video", description="Создать видео"),
    BotCommand(command="music", description="Создать музыку"),
    BotCommand(command="tts", description="Озвучить текст"),
    BotCommand(command="voice", description="Голос и аудио"),
    BotCommand(command="up", description="Улучшить фото"),
    BotCommand(command="clear", description="Очистить память чата"),
]

_MAIN_MENU_TEXT = (
    "<b>Hatani AI</b>\n"
    "Я тут. Пиши задачу нормально или выбирай, что будем разносить.\n\n"
    "<i>Команды — быстрый путь. Кнопки — если не хочешь вспоминать синтаксис.</i>"
)

_GUIDES = {
    "image": (
        "<b>Картинка</b>\n"
        "Отправь <code>/image что нарисовать</code>. Фото или альбом прикрепляй как референс.\n\n"
        "Пример: <code>/image рыжий кот-космонавт, кинопостер</code>"
    ),
    "video": (
        "<b>Видео</b>\n"
        "Отправь <code>/video что должно произойти</code>. Прикрепишь фото — бот его анимирует.\n\n"
        "Рендер может занять до пяти минут, так что не долби кнопку повторно."
    ),
    "music": (
        "<b>Музыка</b>\n"
        "Отправь <code>/music описание трека</code>, затем выбери модель.\n\n"
        "Пример: <code>/music мрачный synthwave без вокала</code>"
    ),
    "figma": (
        "<b>Figma</b>\n"
        "Отправь <code>/figma описание интерфейса</code>. Нужен подключённый Figma Plugin.\n\n"
        "Пример: <code>/figma экран музыкального плеера в тёмной теме</code>"
    ),
    "up": (
        "<b>Апскейл фото</b>\n"
        "Прикрепи одно фото и добавь команду <code>/up</code>. Результат придёт документом без сжатия."
    ),
}

HELP_PAGES = (
    (
        "<b>Быстрый старт</b>\n"
        "Пиши задачу обычным сообщением — агент сам выберет нужный инструмент.\n\n"
        "• Ответить боту: упомяни его или сделай реплай.\n"
        "• Картинка: <code>/image промпт</code>.\n"
        "• Видео: <code>/video промпт</code>.\n"
        "• Если застрял — вернись в <code>/start</code>, а не долби одну кнопку десять раз."
    ),
    (
        "<b>Создание</b>\n"
        "• <code>/image</code> — картинка по тексту или референсу.\n"
        "• <code>/video</code> — видео по тексту или фото.\n"
        "• <code>/music</code> — музыка по описанию.\n"
        "• <code>/figma</code> — макет через подключённый плагин.\n"
        "• <code>/up</code> — улучшить прикреплённое фото без сжатия."
    ),
    (
        "<b>Голос и аудио</b>\n"
        "Открой <code>/voice</code>: там озвучка, клонирование голоса, распознавание речи, очистка шума и Voice Changer.\n\n"
        "Быстрый TTS: <code>/tts текст</code>. Внутри каждого шага есть возврат и отмена."
    ),
    (
        "<b>Агент и инструменты</b>\n"
        "Агент умеет искать свежие данные, скачивать медиа, собирать проекты, анализировать файлы, строить графики, переводить и создавать QR.\n\n"
        "Пиши результат, а не название инструмента: «найди», «скачай», «собери», «проанализируй». Если ничего не найдено — бот обязан сказать это прямо, а не выдумывать херню."
    ),
    (
        "<b>Команды</b>\n"
        "<code>/start</code> меню · <code>/help</code> справка · <code>/clear</code> очистить память\n"
        "<code>/image</code> картинка · <code>/video</code> видео · <code>/music</code> музыка\n"
        "<code>/tts</code> озвучка · <code>/voice</code> Voice Lab · <code>/up</code> апскейл\n\n"
        "Дополнительно: <code>/figma</code>, <code>/bull</code>, <code>/all</code>; <code>/dual</code> — только в специальной беседе."
    ),
)


def _help_view(page: int) -> tuple[str, InlineKeyboardMarkup]:
    page = max(0, min(page, len(HELP_PAGES) - 1))
    rows = []
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="← Раньше", callback_data=f"help:{page - 1}"))
    if page < len(HELP_PAGES) - 1:
        navigation.append(InlineKeyboardButton(text="Дальше →", callback_data=f"help:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([
        InlineKeyboardButton(text="Главное меню", callback_data="menu:home"),
        InlineKeyboardButton(text="✕ Закрыть", callback_data="menu:close"),
    ])
    text = f"{HELP_PAGES[page]}\n\n<i>Страница {page + 1} из {len(HELP_PAGES)}</i>"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

def _clear_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стереть всё", callback_data="menu:clear:confirm")],
        [
            InlineKeyboardButton(text="← Назад", callback_data="menu:tools"),
            InlineKeyboardButton(text="✕ Закрыть", callback_data="menu:close"),
        ],
    ])


async def _clear_chat(chat_id: int) -> None:
    await save_history(chat_id, [])
    chat_context_buffer.pop(chat_id, None)
    chat_last_files.pop(chat_id, None)
    from state import running_agent_tasks
    task = running_agent_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


@core_router.callback_query(F.data == "menu:voice")
async def menu_voice(callback: types.CallbackQuery):
    from handlers.voice import VOICE_MENU_TEXT, _main_menu_keyboard as voice_keyboard
    await callback.answer()
    await callback.message.edit_text(
        VOICE_MENU_TEXT,
        reply_markup=voice_keyboard(),
        parse_mode="HTML",
    )


@core_router.callback_query(F.data == "menu:clear")
async def menu_clear(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "<b>Стереть память чата?</b>\nИстория, контекст и последний файл исчезнут. Назад дороги нет, так что думай.",
        reply_markup=_clear_confirmation_keyboard(),
        parse_mode="HTML",
    )


@core_router.callback_query(F.data == "menu:clear:confirm")
async def menu_clear_confirm(callback: types.CallbackQuery):
    await callback.answer()
    await _clear_chat(callback.message.chat.id)
    await callback.message.edit_text(
        "Память стёрта. Начинай с чистого листа и не тащи старый мусор обратно.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Главное меню", callback_data="menu:home"),
            InlineKeyboardButton(text="✕ Закрыть", callback_data="menu:close"),
        ]]),
    )
@core_router.message(Command('clear'))
async def cmd_clear(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        await message.reply('Доступ закрыт: сначала вступи в обязательную беседу, затем повтори /clear.')
        return
    await message.reply(
        "<b>Стереть память чата?</b>\nИстория, контекст и последний файл исчезнут. Назад дороги нет, так что думай.",
        reply_markup=_clear_confirmation_keyboard(),
        parse_mode='HTML',
    )

@core_router.callback_query(F.data.startswith("help:"))
async def menu_help(callback: types.CallbackQuery):
    await callback.answer()
    page = int(callback.data.split(":", 1)[1])
    text, keyboard = _help_view(page)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
@core_router.message(Command('help'))
async def cmd_help(message: types.Message):
    text, keyboard = _help_view(0)
    await message.reply(text, reply_markup=keyboard, parse_mode='HTML')


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Чат и агент", callback_data="menu:chat"),
            InlineKeyboardButton(text="🎨 Создать", callback_data="menu:create"),
        ],
        [
            InlineKeyboardButton(text="🎙 Голос и аудио", callback_data="menu:voice"),
            InlineKeyboardButton(text="🧰 Инструменты", callback_data="menu:tools"),
        ],
        [InlineKeyboardButton(text="📖 Как пользоваться", callback_data="help:0")],
    ])


def _section_view(section: str) -> tuple[str, InlineKeyboardMarkup]:
    if section == "chat":
        text = (
            "<b>Чат и агент</b>\n"
            "Пиши задачу обычным сообщением. Я сам решу: ответить, поискать, запустить инструмент или собрать файл.\n\n"
            "Примеры: «найди свежие новости», «собери сайт», «скачай видео», «построй график»."
        )
        rows = [[InlineKeyboardButton(text="Возможности агента", callback_data="help:3")]]
    elif section == "create":
        text = "<b>Создание</b>\nВыбирай, что будем создавать, и не тыкай наугад."
        rows = [
            [
                InlineKeyboardButton(text="Картинку", callback_data="guide:image"),
                InlineKeyboardButton(text="Видео", callback_data="guide:video"),
            ],
            [
                InlineKeyboardButton(text="Музыку", callback_data="guide:music"),
                InlineKeyboardButton(text="Figma", callback_data="guide:figma"),
            ],
        ]
    elif section == "tools":
        text = "<b>Инструменты</b>\nБыстрые действия без стены бесполезного текста."
        rows = [
            [InlineKeyboardButton(text="Улучшить фото", callback_data="guide:up")],
            [InlineKeyboardButton(text="Очистить память", callback_data="menu:clear")],
            [InlineKeyboardButton(text="Возможности агента", callback_data="help:3")],
        ]
    else:
        raise ValueError(f"Неизвестный раздел меню: {section}")
    rows.append([
        InlineKeyboardButton(text="← Назад", callback_data="menu:home"),
        InlineKeyboardButton(text="✕ Закрыть", callback_data="menu:close"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _guide_view(name: str) -> tuple[str, InlineKeyboardMarkup]:
    text = _GUIDES.get(name)
    if text is None:
        raise ValueError(f"Неизвестная инструкция: {name}")
    return text, InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="← К разделу", callback_data="menu:create" if name != "up" else "menu:tools"),
        InlineKeyboardButton(text="✕ Закрыть", callback_data="menu:close"),
    ]])


@core_router.callback_query(F.data == "menu:home")
async def menu_home(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        _MAIN_MENU_TEXT,
        reply_markup=_main_menu_keyboard(),
        parse_mode="HTML",
    )


@core_router.callback_query(F.data.in_({"menu:chat", "menu:create", "menu:tools"}))
async def menu_section(callback: types.CallbackQuery):
    await callback.answer()
    text, keyboard = _section_view(callback.data.split(":", 1)[1])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@core_router.callback_query(F.data.startswith("guide:"))
async def menu_guide(callback: types.CallbackQuery):
    await callback.answer()
    text, keyboard = _guide_view(callback.data.split(":", 1)[1])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@core_router.callback_query(F.data == "menu:close")
async def menu_close(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("Меню закрыто. Пиши задачу, когда перестанешь тупить.")
@core_router.message(Command('start'))
async def cmd_start(message: types.Message):
    if message.chat.type == 'private':
        is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
        if not is_member:
            await message.answer('Доступ закрыт: сначала вступи в обязательную беседу, потом возвращайся.')
            return
    await message.answer(
        _MAIN_MENU_TEXT,
        reply_markup=_main_menu_keyboard(),
        parse_mode='HTML',
    )

_ALL_PHRASES = [
    'Эй вы, уроды, все сюда нахуй! 👇',
    'Хуиданте сюда все, живо! 🔔',
    'Ау, дебилы, слышите? Все сюда! 📢',
    'Все ко мне, быстро, я сказал! 🗣️',
    'Ну-ка все собрались, чего расползлись! 👊',
    'Стоять всем! Сюда смотреть! 👁️',
    'Эй ты, и ты, и ты тоже — все на месте! ⚡'
]





@core_router.message(Command('all'))
async def cmd_all(message: types.Message):
    if message.chat.type == 'private':
        await message.reply('В личке некого созывать, дурик.')
        return
    if message.from_user.id != OWNER_USER_ID:
        return
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

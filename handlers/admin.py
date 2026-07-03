import time
import logging
import asyncio
from datetime import date
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_USER_ID, ALLOWED_USER_IDS, DAILY_GEN_LIMIT
from state import (
    banned_user_ids,
    chat_custom_limits,
    daily_gen_limits,
    chat_members_cache,
    paid_unlimited_until,
)
from database import (
    remove_banned_user_db,
    add_banned_user_db,
    set_chat_limit_db,
    get_user_stats,
    get_recent_prompts,
)

admin_router = Router()

@admin_router.message(Command('unban'))
async def cmd_unban(message: types.Message):
    if message.from_user.id != OWNER_USER_ID:
        return
    target_id = None
    parts = (message.text or '').split()
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        if len(parts) > 1:
            if parts[1].startswith('@'):
                target_username = parts[1][1:]
                for (cid, mems) in chat_members_cache.items():
                    for (uid, (_, un)) in mems.items():
                        if un and un.lower() == target_username.lower():
                            target_id = uid
                            break
                    if target_id:
                        break
            else:
                try:
                    target_id = int(parts[1])
                except ValueError:
                    pass
    if not target_id:
        if parts and len(parts) > 1 and parts[1].startswith('@'):
            await message.reply(f'Я не знаю юзера {parts[1]} (нет в кэше). Пусть напишет что-то в чат, или укажи его числовой ID.')
        else:
            await message.reply('Ответь на сообщение юзера или укажи /unban <user_id> или @username')
        return
    await remove_banned_user_db(target_id)
    if target_id in banned_user_ids:
        banned_user_ids.remove(target_id)
    await message.reply(f'✅ Юзер {target_id} разбанен.')

@admin_router.message(Command('ban'))
async def cmd_ban(message: types.Message):
    if message.from_user.id != OWNER_USER_ID:
        return
    target_id = None
    parts = (message.text or '').split()
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        if len(parts) > 1:
            if parts[1].startswith('@'):
                target_username = parts[1][1:]
                for (cid, mems) in chat_members_cache.items():
                    for (uid, (_, un)) in mems.items():
                        if un and un.lower() == target_username.lower():
                            target_id = uid
                            break
                    if target_id:
                        break
            else:
                try:
                    target_id = int(parts[1])
                except ValueError:
                    pass
    if not target_id:
        if parts and len(parts) > 1 and parts[1].startswith('@'):
            await message.reply(f'Я не знаю юзера {parts[1]} (нет в кэше). Пусть напишет что-то в чат, или укажи его числовой ID.')
        else:
            await message.reply('Ответь на сообщение юзера или укажи /ban <user_id> или @username')
        return
    await add_banned_user_db(target_id)
    banned_user_ids.add(target_id)
    await message.reply(f'🚫 Юзер {target_id} забанен навсегда.')

@admin_router.message(Command('limit'))
async def cmd_limit(message: types.Message):
    if message.from_user.id not in ALLOWED_USER_IDS and message.from_user.id != OWNER_USER_ID:
        return
    parts = (message.text or '').split()
    if len(parts) < 3:
        await message.reply('Укажи лимит и количество дней, например: /limit 3 1')
        return
    try:
        req_limit = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await message.reply('Некорректный формат. Нужно: /limit <количество> <дней>')
        return
    chat_custom_limits[message.chat.id] = (req_limit, days)
    await set_chat_limit_db(message.chat.id, req_limit, days)
    await message.reply(f'✅ Установлен лимит для этого чата: {req_limit} генераций раз в {days} дней.')

def _stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='За сегодня', callback_data='stats:today')], [InlineKeyboardButton(text='За всё время', callback_data='stats:all')]])

@admin_router.message(Command('stats'))
async def cmd_stats(message: types.Message):
    if message.from_user.id != OWNER_USER_ID:
        return
    await message.reply('📊 Выберите период для статистики:', reply_markup=_stats_keyboard())

@admin_router.callback_query(F.data.startswith('stats:'))
async def handle_stats(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_USER_ID:
        await callback.answer('Только для владельца.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    period = parts[1]
    today_str = str(date.today())
    if period == 'today':
        stats = await get_user_stats(today_str)
        title = f'📊 Статистика за сегодня ({today_str}):'
    else:
        stats = await get_user_stats()
        title = '📊 Статистика за всё время:'
    if not stats:
        await callback.answer('Нет данных.')
        return
    lines = [title, '']
    user_totals = {}
    for s in stats:
        uid = s['user_id']
        if uid not in user_totals:
            user_totals[uid] = {'name': s['first_name'], 'username': s['username'], 'image': 0, 'video': 0, 'text': 0, 'code': 0}
        t = s['type']
        if t in user_totals[uid]:
            user_totals[uid][t] += s['count']
    sorted_users = sorted(user_totals.items(), key=lambda x: sum((x[1][k] for k in ['image', 'video', 'text', 'code'])), reverse=True)
    for (uid, data) in sorted_users:
        un = f"@{data['username']}" if data['username'] else f"<a href='tg://user?id={uid}'>{data['name']}</a>"
        total = sum((data[k] for k in ['image', 'video', 'text', 'code']))
        line = f'👤 {un} (<code>{uid}</code>)\n'
        line += f"  Всего: {total} (Картинки: {data['image']}, Видео: {data['video']}, Текст: {data['text']}, Код: {data['code']})"
        lines.append(line)
    text = '\n\n'.join(lines)[:4000]
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=_stats_keyboard())
    except Exception as e:
        logging.warning(f'Ошибка отображения статистики: {e}')
    await callback.answer()

@admin_router.message(Command('prompts'))
async def cmd_prompts(message: types.Message):
    if message.from_user.id != OWNER_USER_ID:
        return
    parts = (message.text or '').split()
    user_id = None
    if len(parts) > 1:
        try:
            user_id = int(parts[1])
        except ValueError:
            await message.reply('Укажи валидный ID юзера, например: /prompts 12345678')
            return
    prompts = await get_recent_prompts(limit=15, user_id=user_id)
    if not prompts:
        await message.reply('Нет логов промптов.')
        return
    lines = [f'📝 Последние {len(prompts)} промптов' + (f' от {user_id}' if user_id else '') + ':\n']
    for p in prompts:
        import datetime
        dt = datetime.datetime.fromtimestamp(p['created_at']).strftime('%H:%M:%S')
        un = f"@{p['username']}" if p['username'] else f"{p['first_name']}"
        lines.append(f"[{dt}] 👤 {un} ({p['user_id']}) - <b>{p['gen_type']}</b>:\n<pre>{p['prompt'][:300]}</pre>")
    text = '\n\n'.join(lines)[:4000]
    await message.reply(text, parse_mode='HTML')

@admin_router.message(Command('vip'))
async def cmd_vip(message: types.Message):
    if message.from_user.id not in ALLOWED_USER_IDS and message.from_user.id != OWNER_USER_ID:
        return
    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        parts = (message.text or '').split()
        if len(parts) > 1:
            try:
                target_id = int(parts[1])
            except ValueError:
                pass
    if not target_id:
        await message.reply('Ответь на сообщение юзера или укажи /vip <user_id>')
        return
    paid_unlimited_until[target_id] = time.time() + 86400
    await message.reply(f'✅ Юзер {target_id} получил безлимит на 24 часа.')

def _check_daily_limit(user_id: int, chat_id: int) -> tuple:
    (req_limit, days) = chat_custom_limits.get(chat_id, (DAILY_GEN_LIMIT, 1))
    period_id = str(int(time.time() // (86400 * days))) if days > 0 else str(time.time())
    entry = daily_gen_limits.get((chat_id, user_id), {})
    if entry.get('period') != period_id:
        entry = {'period': period_id, 'count': 0}
    count = entry.get('count', 0)
    if count >= req_limit:
        return (False, 0)
    entry['count'] = count + 1
    daily_gen_limits[chat_id, user_id] = entry
    return (True, req_limit - entry['count'])

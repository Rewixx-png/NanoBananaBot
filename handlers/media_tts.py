import time
import uuid
import asyncio
import logging

from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from state import (
    user_tts_cooldowns,
    pending_tts_requests,
    pending_tts_configs,
    tts_voice_previews,
)
from services.audio_service import generate_tts_with_gemini
from utils import check_membership
from handlers.common import (
    _track_user,
    _tts_cfg_text,
    _tts_cfg_keyboard,
)
from handlers.media_gen import media_router, TTS_MODELS


_tts_awaiting_input: dict = {}
TTS_COOLDOWN_SECONDS = 10

def _tts_model_keyboard(request_id: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f'ttssel:{request_id}:{model_id}')] for (model_id, (label, _)) in TTS_MODELS.items()]
    rows.append([InlineKeyboardButton(text='Отмена', callback_data=f'ttsabort:{request_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@media_router.message(Command('tts'))
async def cmd_tts(message: types.Message):
    _track_user(message)
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member and message.chat.type != 'private':
        await message.reply('Доступ закрыт: сначала вступи в обязательную беседу, потом озвучивай.')
        return
    prompt = (message.text or '').replace('/tts', '').strip()
    if not prompt:
        await message.reply('Текст пустой. Напиши, что озвучить: /tts Привет, ублюдок!')
        return
    if not TTS_MODELS:
        await message.reply('TTS-модели не загрузились. Попробуй /tts позже; повторный тык сейчас ничего не исправит.')
        return
    current_time = time.time()
    last_time = user_tts_cooldowns.get(message.from_user.id, 0)
    if current_time - last_time < TTS_COOLDOWN_SECONDS:
        await message.reply(f'Озвучка уже запускалась. Подожди ещё {int(TTS_COOLDOWN_SECONDS - (current_time - last_time))} сек.')
        return
    user_tts_cooldowns[message.from_user.id] = current_time
    request_id = uuid.uuid4().hex[:10]
    thread_id = message.message_thread_id if message.chat.is_forum else None
    pending_tts_requests[request_id] = {'user_id': message.from_user.id, 'chat_id': message.chat.id, 'source_message_id': message.message_id, 'message_thread_id': thread_id, 'prompt': prompt, 'username': message.from_user.username or '', 'first_name': message.from_user.first_name or 'Аноним'}
    keyboard = _tts_model_keyboard(request_id)
    reply_kwargs = {}
    if message.chat.is_forum and thread_id:
        reply_kwargs['message_thread_id'] = thread_id
    await message.reply('Выбирай TTS-модель. Не знаешь разницу — бери Flash.', reply_markup=keyboard, **reply_kwargs)

@media_router.callback_query(F.data.startswith('ttsabort:'))
async def handle_tts_abort(callback: types.CallbackQuery):
    request_id = (callback.data or '').removeprefix('ttsabort:')
    request_data = pending_tts_requests.get(request_id) or pending_tts_configs.get(request_id)
    if not request_data:
        await callback.answer('Запрос уже завершён.', show_alert=True)
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Только автор запроса может его отменить.', show_alert=True)
        return
    pending_tts_requests.pop(request_id, None)
    pending_tts_configs.pop(request_id, None)
    _tts_awaiting_input.pop((request_data['chat_id'], request_data['user_id']), None)
    await callback.answer('Отменено')
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='← К созданию', callback_data='menu:create')]])
    await callback.message.edit_text('Генерация аудио отменена.', reply_markup=keyboard)


@media_router.callback_query(F.data.startswith('ttsback:'))
async def handle_tts_back(callback: types.CallbackQuery):
    request_id = (callback.data or '').removeprefix('ttsback:')
    config = pending_tts_configs.get(request_id)
    if not config:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != config['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    request_data = {key: value for key, value in config.items() if key not in {'model', 'label', 'cfg'}}
    try:
        await callback.message.edit_text('Выбирай TTS-модель.', reply_markup=_tts_model_keyboard(request_id))
    except Exception as e:
        logging.warning(f'TTS back edit failed: {type(e).__name__}: {e}', exc_info=True)
        await callback.answer(f'Не удалось вернуться: {type(e).__name__}: {e}', show_alert=True)
        return
    pending_tts_configs.pop(request_id, None)
    _tts_awaiting_input.pop((config['chat_id'], config['user_id']), None)
    pending_tts_requests[request_id] = request_data
    await callback.answer()


@media_router.callback_query(F.data.startswith('ttssel:'))
async def handle_tts_model_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    (_, request_id, model_id) = parts
    request_data = pending_tts_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел. Отправьте /tts заново.', show_alert=True)
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    model_info = TTS_MODELS.get(model_id)
    if not model_info:
        await callback.answer('Неизвестная модель.', show_alert=True)
        return
    (model_label, real_model) = model_info
    pending_tts_requests.pop(request_id, None)
    pending_tts_configs[request_id] = {**request_data, 'model': real_model, 'label': model_label, 'cfg': {'voice': 'Puck'}}
    await callback.answer()
    try:
        await callback.message.edit_text(_tts_cfg_text(request_id), reply_markup=_tts_cfg_keyboard(request_id))
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('ttsinput:'))
async def handle_tts_input(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 3:
        return
    (_, request_id, field) = parts
    d = pending_tts_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await callback.answer()
    field_prompts = {'prompt': '✏️ Напиши **текст для озвучки** следующим сообщением:\n\n(просто отправь текст в чат)', 'scene': '🎭 Опиши **сцену (окружение)** следующим сообщением:\n\nЭто задаст общий вайб и акустику. Примеры:\n🇷🇺 <i>Шумное кафе ранним утром, играет тихий джаз на фоне.</i>\n🇬🇧 <i>A busy train station with echoing announcements.</i>\n\n(отправь текст или нажми Отмена)', 'style': '🎭 Опиши **стиль (настроение)** следующим сообщением:\n\nУказывает эмоцию и характер речи. Примеры:\n🇷🇺 <i>радостно, агрессивно, шепотом, уставший.</i>\n🇬🇧 <i>energetic and upbeat, angry, whispering, tired.</i>\n\n(отправь текст или нажми Отмена)', 'pace': '🎭 Укажи **темп речи** следующим сообщением:\n\nС какой скоростью говорить. Примеры:\n🇷🇺 <i>очень быстро, медленно с длинными паузами, размеренно.</i>\n🇬🇧 <i>very fast, slow with dramatic pauses, steady.</i>\n\n(отправь текст или нажми Отмена)', 'accent': '🎭 Укажи **акцент или манеру** следующим сообщением:\n\nПримеры:\n🇷🇺 <i>с британским акцентом, французский акцент, грубый голос.</i>\n🇬🇧 <i>British accent, Southern US drawl, French accent.</i>\n\n(отправь текст или нажми Отмена)'}
    prompt_text = field_prompts.get(field, f'✏️ Напиши {field} следующим сообщением:')
    _tts_awaiting_input[d['chat_id'], d['user_id']] = {'request_id': request_id, 'field': field, 'msg_id': callback.message.message_id}
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data=f'ttscancel:{request_id}')]])
    try:
        await callback.message.edit_text(prompt_text, reply_markup=cancel_kb, parse_mode='HTML')
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('ttscancel:'))
async def handle_tts_cancel(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_tts_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
    _tts_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    await callback.answer('Отменено')
    try:
        await callback.message.edit_text(_tts_cfg_text(request_id), reply_markup=_tts_cfg_keyboard(request_id))
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('ttscfg:'))
async def handle_tts_config(callback: types.CallbackQuery):
    parts = callback.data.split(':', 3)
    if len(parts) != 4:
        await callback.answer()
        return
    (_, request_id, field, value) = parts
    d = pending_tts_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    if field == 'voice':
        d['cfg']['voice'] = value
    elif field == 'temp':
        d['cfg']['temp'] = float(value)
    elif field == 'lang':
        d['cfg']['lang'] = value
    await callback.answer()
    try:
        await callback.message.edit_text(_tts_cfg_text(request_id), reply_markup=_tts_cfg_keyboard(request_id))
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('ttsprev:'))
async def handle_tts_preview(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_tts_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
    voice = d['cfg'].get('voice', 'Puck')
    lang = d['cfg'].get('lang', 'ru-RU')
    temp = d['cfg'].get('temp', 1.0)
    cache_key = f'{voice}_{lang}_{temp}'
    cached_file_id = tts_voice_previews.get(cache_key)
    await callback.answer()
    reply_kwargs = {'message_thread_id': d.get('message_thread_id')} if d.get('message_thread_id') else {}
    if cached_file_id:
        await callback.bot.send_voice(chat_id=d['chat_id'], voice=cached_file_id, caption=f'🔊 Проверка: {voice} ({lang})', reply_to_message_id=d['source_message_id'], **reply_kwargs)
        return
    await callback.bot.send_chat_action(chat_id=d['chat_id'], action='record_voice', message_thread_id=d.get('message_thread_id'))
    test_phrases = {'ru-RU': 'Привет! Это проверка моего голоса. Как меня слышно?', 'en-US': 'Hello! This is a test of my voice. How do I sound?', 'ja-JP': 'こんにちは！これは私の声のテストです。どう聞こえますか？'}
    phrase = test_phrases.get(lang, test_phrases['ru-RU'])
    (audio_bytes, error_msg) = await generate_tts_with_gemini(phrase, d['model'], voice, temp, lang)
    if error_msg:
        await callback.bot.send_message(chat_id=d['chat_id'], text=f'❌ Ошибка предпрослушивания:\n{error_msg}', reply_to_message_id=d['source_message_id'], **reply_kwargs)
        return
    if audio_bytes:
        voice_file = BufferedInputFile(audio_bytes, filename='voice.ogg')
        sent_msg = await callback.bot.send_voice(chat_id=d['chat_id'], voice=voice_file, caption=f'🔊 Проверка: {voice} ({lang})', reply_to_message_id=d['source_message_id'], **reply_kwargs)
        if sent_msg.voice and sent_msg.voice.file_id:
            tts_voice_previews[cache_key] = sent_msg.voice.file_id

@media_router.callback_query(F.data.startswith('ttsgen:'))
async def handle_tts_generate(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_tts_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
    pending_tts_configs.pop(request_id, None)
    await callback.answer()
    reply_kwargs = {'message_thread_id': d.get('message_thread_id')} if d.get('message_thread_id') else {}
    try:
        await callback.message.edit_text(f"⏳ Генерирую аудио через {d['label']}...")
    except Exception:
        pass
    await callback.bot.send_chat_action(chat_id=d['chat_id'], action='record_voice', message_thread_id=d.get('message_thread_id'))
    cfg = d.get('cfg', {})
    (audio_bytes, error_msg) = await generate_tts_with_gemini(d['prompt'], d['model'], cfg.get('voice', 'Puck'), cfg.get('temp', 1.0), cfg.get('lang', 'ru-RU'))
    try:
        await callback.message.delete()
    except Exception:
        pass
    if error_msg:
        await callback.bot.send_message(chat_id=d['chat_id'], text=f'❌ Ошибка генерации аудио:\n{error_msg}', reply_to_message_id=d['source_message_id'], **reply_kwargs)
        return
    if audio_bytes:
        voice_file = BufferedInputFile(audio_bytes, filename='voice.ogg')
        await callback.bot.send_voice(chat_id=d['chat_id'], voice=voice_file, caption=f"🎙️ {d['label']} | Голос: {d['cfg'].get('voice', 'Puck')}", reply_to_message_id=d['source_message_id'], **reply_kwargs)
        from database import add_user_stat, log_prompt
        asyncio.create_task(add_user_stat(d['user_id'], d.get('username', ''), d.get('first_name', 'Аноним'), 'audio'))
        asyncio.create_task(log_prompt(d['user_id'], d.get('username', ''), d.get('first_name', 'Аноним'), 'audio', d['prompt']))
    else:
        await callback.bot.send_message(chat_id=d['chat_id'], text='❌ Не удалось получить аудио.', reply_to_message_id=d['source_message_id'], **reply_kwargs)

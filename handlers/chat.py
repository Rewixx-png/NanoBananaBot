import asyncio
import re
import uuid
import tempfile
import os
import time
import io
import logging
from typing import Any
from aiogram import Router, F, types
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from handlers.common import (
    safe_send,
    _clean_plain_reply,
    _md_to_html,
    _code_block_ext,
    _maybe_send_random_chat_media,
    _track_user,
    _remember_generated_code_message,
)

from handlers.text_inputs import (
    handle_pending_file_task,
    handle_nsfw_input,
    handle_tts_input,
)
from handlers.agent_cb import send_agent_callback

from database import (
    log_prompt,
    add_user_stat
)

from config import (
    TEXT_ONLY_CHAT_ID,
    FULL_ACCESS_CHAT_ID,
    ALLOWED_USER_IDS,
    ADMIN_IDS,
    TEXT_COOLDOWN_SECONDS,
    FIGMA_TOKEN
)

from state import (
    pending_image_requests,
    pending_video_requests,
    pending_media_groups,
    user_text_cooldowns,
    banned_user_ids,
    pending_prompt_requests,
    pending_tts_requests,
    generated_draw_messages,
    generated_code_messages,
    chat_context_buffer,
    chat_last_files,
    chat_workspaces
)

from ai_services import (
    analyze_photo_with_gemini,
    analyze_voice_with_gemini,
    generate_video_with_gemini,
    upscale_image,
)
from esrgan_model import upscale_anime

from agent import run_agent

from dual_bot import (
    start_dual,
    stop_dual,
    BOT1_DUAL_NAME,
    BOT2_DUAL_NAME
)

from utils import (
    check_membership,
    is_banned,
    make_safe_caption,
)
logger = logging.getLogger(__name__)
chat_router = Router()

_CODE_MODIFY_WORDS = [
    'адаптируй', 'измени', 'добавь', 'убери', 'исправь', 'переделай', 'улучши',
    'замени', 'поправь', 'обнови', 'перепиши', 'доработай', 'дополни', 'сделай',
    'adapt', 'modify', 'change', 'add', 'remove', 'fix', 'improve', 'update', 'refactor',
    'зипку', 'zip', 'архив', 'файлы', 'отправь', 'скинь', 'норм', 'монолит',
    'где', 'дай', 'покажи', 'пришли', 'заново', 'снова', 'ещё раз', 'не пришло', 'пересобери',
]
from handlers.media_in import _send_generated_project, _process_file_task, _handle_file_document_upload, _handle_kick_directive, _media_to_agent


@chat_router.message(F.photo & ~F.caption.startswith('/'))
async def handle_album_photo(message: types.Message):
    if not message.media_group_id:
        bot_user = await message.bot.get_me()
        is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
        is_mentioned = bool(bot_user.username and f'@{bot_user.username}' in (message.caption or ''))
        is_private = message.chat.type == 'private'
        if not is_private and not is_reply_to_bot and not is_mentioned:
            return
        is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
        if not is_member:
            return
        reply_kwargs = {}
        if message.chat.is_forum and message.message_thread_id:
            reply_kwargs['message_thread_id'] = message.message_thread_id
        prompt = message.caption or ''
        if bot_user.username:
            prompt = prompt.replace(f'@{bot_user.username}', '').strip()
        photo = message.photo[-1]
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        image_bytes = downloaded.read()
        if await _media_to_agent(message, image_bytes, 'photo.jpg', prompt, reply_kwargs):
            return
        if not prompt:
            prompt = 'Что на этом фото?'
        wait_msg = await message.reply('⏳ Смотрю на твою хуйню...', **reply_kwargs)
        await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)
        response = await analyze_photo_with_gemini(image_bytes, prompt)
        await wait_msg.delete()
        await message.reply(response or 'Нихуя не понял что это такое.', **reply_kwargs)
        asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
        asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))
        return
    group_id = message.media_group_id
    if group_id not in pending_media_groups:
        return
    photo = message.photo[-1]
    try:
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        group = pending_media_groups[group_id]
        group['images'].append(downloaded.read())
        group.setdefault('file_ids', []).append(photo.file_id)
    except Exception as e:
        logger.warning(f'Ошибка группировки фото: {e}')
@chat_router.message(F.voice | F.audio | F.video_note)
async def handle_voice_audio(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
    bot_user = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bool(bot_user.username and f'@{bot_user.username}' in (message.caption or ''))
    is_private = message.chat.type == 'private'
    if not is_private and not is_reply_to_bot and not is_mentioned:
        return
    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs['message_thread_id'] = message.message_thread_id
    prompt = message.caption or ''
    if bot_user.username:
        prompt = prompt.replace(f'@{bot_user.username}', '').strip()
    if message.video_note:
        if not prompt:
            prompt = 'Что происходит в этом видео?'
        wait_msg = await message.reply('⏳ Смотрю твоё кружочек-видео...', **reply_kwargs)
        file_info = await message.bot.get_file(message.video_note.file_id)
        (_, temp_path) = tempfile.mkstemp(suffix='.mp4')
        await message.bot.download_file(file_info.file_path, destination=temp_path)
        await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)
        response = await generate_video_with_gemini(prompt, temp_path)
        if os.path.exists(temp_path):
            os.remove(temp_path)
        await wait_msg.delete()
        await message.reply(response or 'Нихуя не понял.', **reply_kwargs)
        asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
        asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))
        return
    media = message.voice or message.audio
    mime_type = 'audio/ogg' if message.voice else (media.mime_type or 'audio/mpeg')
    file_info = await message.bot.get_file(media.file_id)
    downloaded = await message.bot.download_file(file_info.file_path)
    audio_bytes = downloaded.read()
    ext = 'ogg' if message.voice else 'mp3'
    if prompt and await _media_to_agent(message, audio_bytes, f'audio.{ext}', prompt, reply_kwargs):
        return
    if not prompt:
        prompt = 'Что сказано в этом голосовом? Транскрибируй и ответь по существу.'
    wait_msg = await message.reply('⏳ Слушаю твою хуйню...', **reply_kwargs)
    await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)
    response = await analyze_voice_with_gemini(audio_bytes, mime_type, prompt)
    await wait_msg.delete()
    await message.reply(response or 'Нихуя не расслышал.', **reply_kwargs)
    asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
    asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))

@chat_router.message(F.video | F.animation | F.document)
async def handle_video(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
    bot_user = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bool(bot_user.username and f'@{bot_user.username}' in (message.caption or ''))
    is_private = message.chat.type == 'private'
    if not is_private and not is_reply_to_bot and not is_mentioned:
        return
    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs['message_thread_id'] = message.message_thread_id
    vid = message.video or message.animation
    if not vid and message.document:
        if not message.document.mime_type or not message.document.mime_type.startswith('video/'):
            await _handle_file_document_upload(message, bot_user, reply_kwargs)
            return
        vid = message.document
    prompt = message.caption or ''
    if bot_user.username:
        prompt = prompt.replace(f'@{bot_user.username}', '').strip()
    if prompt:
        file_info_pre = await message.bot.get_file(vid.file_id)
        if vid.file_size and vid.file_size < 20 * 1024 * 1024:
            buf_pre = await message.bot.download_file(file_info_pre.file_path)
            vid_bytes = buf_pre.read()
            ext = 'mp4'
            if await _media_to_agent(message, vid_bytes, f'video.{ext}', prompt, reply_kwargs):
                return
    if not prompt:
        prompt = 'Внимательно посмотри это видео и скажи, что здесь происходит.'
    wait_msg = await message.reply('⏳ Изучаю твое всратое видео кадр за кадром (24 FPS)...')
    file_info = await message.bot.get_file(vid.file_id)
    (_, temp_vid_path) = tempfile.mkstemp(suffix='.mp4')
    await message.bot.download_file(file_info.file_path, destination=temp_vid_path)
    await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)
    text_response = await generate_video_with_gemini(prompt, temp_vid_path)
    if os.path.exists(temp_vid_path):
        os.remove(temp_vid_path)
    await wait_msg.delete()
    code_blocks = re.findall('```(\\w*)\\n(.*?)```', text_response, re.DOTALL)
    cleaned_text = re.sub('```(\\w*)\\n(.*?)```', '', text_response, flags=re.DOTALL).strip()
    if not cleaned_text and code_blocks:
        cleaned_text = 'Вот твой ебаный код, подавись нахуй.'
    elif not cleaned_text:
        cleaned_text = 'Нихуя не понял, но иди в пизду.'
    sent_msg = await message.reply(cleaned_text, **reply_kwargs)
    if code_blocks:
        for (lang, code) in code_blocks:
            ext = _code_block_ext(lang)
            filename = f'говняный_код_{uuid.uuid4().hex[:4]}.{ext}'
            doc = BufferedInputFile(code.strip().encode('utf-8'), filename=filename)
            await message.bot.send_document(chat_id=message.chat.id, document=doc, reply_to_message_id=sent_msg.message_id, **reply_kwargs)
    asyncio.create_task(add_user_stat(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text'))
    asyncio.create_task(log_prompt(message.from_user.id, message.from_user.username or '', message.from_user.first_name or 'Аноним', 'text', prompt))
@chat_router.message(F.text)
async def handle_text_messages(message: types.Message):
    if message.from_user and message.from_user.is_bot:
        return
    _track_user(message)
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        return
    reply_kwargs: dict = {}
    if await handle_pending_file_task(message, reply_kwargs):
        return
    if await handle_nsfw_input(message, reply_kwargs):
        return
    if await handle_tts_input(message, reply_kwargs):
        return
    asyncio.create_task(_maybe_send_random_chat_media(message))
    bot_user = await message.bot.get_me()
    is_reply_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_user.id
    is_mentioned = bot_user.username and f'@{bot_user.username}' in message.text
    is_private = message.chat.type == 'private'
    if is_reply_to_bot or is_mentioned or is_private:
        current_time = time.time()
        last_time = user_text_cooldowns.get(message.from_user.id, 0)
        if current_time - last_time < TEXT_COOLDOWN_SECONDS:
            await message.reply(f'Заебал строчить, подожди еще {int(TEXT_COOLDOWN_SECONDS - (current_time - last_time))} сек.')
            return
        user_text_cooldowns[message.from_user.id] = current_time
        prompt = message.text
        if bot_user.username:
            prompt = prompt.replace(f'@{bot_user.username}', '').strip()
        if not prompt:
            prompt = 'Что тебе надо, хуйло?'
        web_query = prompt
        if is_reply_to_bot and message.reply_to_message.text:
            replied_text = message.reply_to_message.text[:500]
            prompt = f'[Контекст — ты написал ранее: «{replied_text}»]\n{prompt}'
        replied_code = None
        is_code_forced = False
        if is_reply_to_bot and message.reply_to_message:
            replied_code = generated_code_messages.get((message.chat.id, message.reply_to_message.message_id))
            if replied_code:
                files_ctx = '\n\n'.join(
                    f'### {f["path"]}\n```\n{f["content"][:4000]}\n```'
                    for f in replied_code.get('files', [])
                )
                prev_prompt = replied_code.get('prompt', '')
                prompt = (
                    f'[Контекст — ты ранее сгенерировал этот код по запросу: «{prev_prompt[:300]}»]\n'
                    f'[Файлы которые ты отправил:]\n{files_ctx}\n\n'
                    f'[Новый запрос/правки пользователя:]\n{web_query}'
                )
                is_code_forced = (
                    any(w in web_query.lower() for w in _CODE_MODIFY_WORDS) or
                    len(web_query.strip()) <= 10
                )
        username = message.from_user.first_name or message.from_user.username or 'Аноним'
        reply_kwargs = {}
        if message.chat.is_forum and message.message_thread_id:
            reply_kwargs['message_thread_id'] = message.message_thread_id
        thinking_msg = await message.reply('⏳ Думаю...', **reply_kwargs)
        await message.bot.send_chat_action(chat_id=message.chat.id, action='typing', message_thread_id=message.message_thread_id if message.chat.is_forum else None)

        last_status_edit = 0.0
        last_status_text = ''

        async def _status_cb(text: str):
            nonlocal last_status_edit, last_status_text
            now = time.monotonic()
            if text == last_status_text or now - last_status_edit < 1.5:
                return
            try:
                await thinking_msg.edit_text(text, parse_mode='HTML')
                last_status_edit = time.monotonic()
                last_status_text = text
            except TelegramRetryAfter as e:
                last_status_edit = time.monotonic() + float(getattr(e, 'retry_after', 3) or 3)
            except Exception:
                try:
                    import re as _re
                    await thinking_msg.edit_text(_re.sub(r'<[^>]+>', '', text))
                except Exception as e:
                    logger.warning(f'Ошибка обновления статуса (plain): {e}')

        replied_draw = None
        if is_reply_to_bot and message.reply_to_message and message.reply_to_message.photo:
            replied_draw = generated_draw_messages.get((message.chat.id, message.reply_to_message.message_id))
            if replied_draw is None:
                replied_image = await _download_message_photo(message.bot, message.reply_to_message)
                if replied_image:
                    replied_draw = {'image_bytes': replied_image, 'prompt': message.reply_to_message.caption or ''}
            if replied_draw:
                prompt = (
                    f'[Пользователь отвечает на сгенерированную картинку. '
                    f'Оригинальный промпт: «{replied_draw.get("prompt", "")}»]\n{prompt}'
                )

        async def _agent_send_cb(media: dict):
            await send_agent_callback(media, message=message, reply_kwargs=reply_kwargs)

        is_owner_user = message.from_user.id in ADMIN_IDS

        from state import chat_last_files as _clf
        _cached = _clf.get(message.chat.id)
        _initial_files = {}
        if _cached and time.time() - _cached["ts"] < 3600:
            _safe_name = os.path.basename(_cached["filename"]) or "upload"
            _ws_path = os.path.realpath(f"/workspace/{_safe_name}")
            if _ws_path.startswith("/workspace/"):
                _initial_files = {_safe_name: _cached["data"]}
                prompt = (f'[Ранее в этом чате был загружен файл: {_safe_name}. '
                          f'Он уже доступен в workspace как /workspace/{_safe_name}]\n') + prompt

        try:
            (agent_text, agent_project) = await asyncio.wait_for(
                run_agent(prompt, message.chat.id, username, _status_cb, _agent_send_cb,
                          is_owner=is_owner_user,
                          initial_files=_initial_files if _initial_files else None),
                timeout=1200,
            )
        except asyncio.TimeoutError:
            agent_text = 'Завис по таймауту. Попробуй покороче.'
            agent_project = None
        except Exception as _agent_err:
            logger.exception(f'Agent crashed: {_agent_err}')
            agent_text = 'Агент упал. Попробуй ещё раз.'
            agent_project = None

        try:
            await thinking_msg.delete()
        except Exception as e:
            logger.warning(f'Ошибка удаления thinking-сообщения: {e}')

        if agent_project:
            sent_code_doc = await _send_generated_project(message, agent_project, reply_kwargs)
            if sent_code_doc:
                _remember_generated_code_message(message.chat.id, sent_code_doc.message_id, agent_project.get('files', []), web_query)
        elif agent_text:
            agent_text = await _handle_kick_directive(message, agent_text, reply_kwargs)
            code_blocks = re.findall('```(\\w*)\\n(.*?)```', agent_text, re.DOTALL)
            html_text = re.sub('```(\\w*)\\n(.*?)```', '', agent_text, flags=re.DOTALL).strip()
            if not html_text and code_blocks:
                html_text = 'Вот твой ебаный код, подавись нахуй.'
            elif not html_text:
                html_text = 'Нихуя не понял, но иди в пизду.'
            try:
                import bleach
                _TG_TAGS = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
                            'code', 'pre', 'blockquote', 'tg-spoiler', 'tg-emoji']
                safe_html = bleach.clean(
                    _md_to_html(html_text),
                    tags=_TG_TAGS,
                    attributes={'pre': [], 'code': ['class'], 'tg-emoji': ['emoji-id'],
                                'blockquote': ['expandable']},
                    strip=True,
                )
                sent_msg = await safe_send(message.reply, safe_html, parse_mode='HTML', **reply_kwargs)
            except Exception:
                sent_msg = await safe_send(message.reply, _clean_plain_reply(html_text), **reply_kwargs)
            if not sent_msg:
                logger.warning('Agent reply not sent after flood-control retries')
                return
            if code_blocks:
                _code_files_sent = []
                for (lang, code) in code_blocks:
                    ext = _code_block_ext(lang)
                    filename = f'говняный_код_{uuid.uuid4().hex[:4]}.{ext}'
                    doc = BufferedInputFile(code.strip().encode('utf-8'), filename=filename)
                    sent_code_file = await safe_send(message.bot.send_document, chat_id=message.chat.id, document=doc, reply_to_message_id=sent_msg.message_id, **reply_kwargs)
                    file_entry = {'path': filename, 'content': code.strip()}
                    _code_files_sent.append(file_entry)
                    if sent_code_file:
                        _remember_generated_code_message(message.chat.id, sent_code_file.message_id, [file_entry], web_query)
                if sent_msg and _code_files_sent:
                    _remember_generated_code_message(message.chat.id, sent_msg.message_id, _code_files_sent, web_query)

        if agent_text:
            from state import chat_context_buffer as _ccb2
            from config import MAX_HISTORY_MESSAGES as _mhm2
            _buf2 = _ccb2.setdefault(message.chat.id, [])
            _buf2.append(f"Hatani: {agent_text[:500]}")
            if len(_buf2) > _mhm2:
                _ccb2[message.chat.id] = _buf2[-_mhm2:]

        asyncio.create_task(add_user_stat(message.from_user.id, username, message.from_user.first_name or 'Аноним', 'text'))
        asyncio.create_task(log_prompt(message.from_user.id, username, message.from_user.first_name or 'Аноним', 'text', web_query))

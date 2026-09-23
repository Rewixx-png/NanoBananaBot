"""State-machine text input handlers extracted from chat.py handle_text_messages.

Each function returns True if it consumed the message; False otherwise.
"""

from __future__ import annotations

from aiogram import types

from handlers.common import (
    _nsfw_cfg_text,
    _nsfw_cfg_keyboard,
    _tts_cfg_text,
    _tts_cfg_keyboard,
    _suno_cfg_text,
    _suno_cfg_keyboard,
    _lyria_cfg_text,
    _lyria_cfg_keyboard,
)
from handlers.media_gen import _nsfw_awaiting_input
from handlers.media_tts import _tts_awaiting_input
from handlers.music import _suno_awaiting_input, _lyria_awaiting_input
from state import (
    pending_file_tasks,
    pending_nsfw_configs,
    pending_tts_configs,
    pending_suno_configs,
    pending_lyria_configs,
)


async def handle_pending_file_task(message: types.Message, reply_kwargs: dict) -> bool:
    """Consume a pending file-task instruction. Returns True if consumed."""
    _file_key = (message.chat.id, message.from_user.id)
    if _file_key not in pending_file_tasks:
        return False
    task_data = pending_file_tasks.pop(_file_key)
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs['message_thread_id'] = message.message_thread_id
    from handlers.media_in import _process_file_task
    await _process_file_task(message, task_data, message.text.strip(), reply_kwargs)
    return True


async def handle_nsfw_input(message: types.Message, reply_kwargs: dict) -> bool:  # noqa: ARG001
    """Consume NSFW config field input. Returns True if consumed."""
    _nsfw_key = (message.chat.id, message.from_user.id)
    if _nsfw_key not in _nsfw_awaiting_input:
        return False
    wait = _nsfw_awaiting_input.pop(_nsfw_key)
    request_id: str = wait['request_id']
    field: str = wait['field']
    msg_id: int = wait['msg_id']
    d = pending_nsfw_configs.get(request_id)
    if d:
        new_val = message.text.strip()
        if field == 'prompt':
            d['prompt'] = new_val
        elif field == 'neg':
            d['cfg']['neg'] = new_val
        elif field == 'seed':
            try:
                d['cfg']['seed'] = int(new_val)
            except ValueError:
                d['cfg']['seed'] = -1
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text=_nsfw_cfg_text(request_id),
                reply_markup=_nsfw_cfg_keyboard(request_id),
            )
        except Exception:
            await message.bot.send_message(
                chat_id=d['chat_id'],
                text=_nsfw_cfg_text(request_id),
                reply_markup=_nsfw_cfg_keyboard(request_id),
            )
    return True


async def handle_suno_input(message: types.Message, reply_kwargs: dict) -> bool:  # noqa: ARG001
    """Consume Suno config field input. Returns True if consumed."""
    _suno_key = (message.chat.id, message.from_user.id)
    if _suno_key not in _suno_awaiting_input:
        return False
    wait = _suno_awaiting_input.pop(_suno_key)
    request_id: str = wait['request_id']
    field: str = wait['field']
    msg_id: int = wait['msg_id']
    d = pending_suno_configs.get(request_id)
    if d:
        new_val = message.text.strip()
        if field == 'title':
            new_val = new_val[:80]
        d['cfg'][field] = new_val
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text=_suno_cfg_text(request_id),
                reply_markup=_suno_cfg_keyboard(request_id),
                parse_mode='HTML',
            )
        except Exception:
            await message.bot.send_message(
                chat_id=d['chat_id'],
                text=_suno_cfg_text(request_id),
                reply_markup=_suno_cfg_keyboard(request_id),
                parse_mode='HTML',
            )
    return True


async def handle_lyria_input(message: types.Message, reply_kwargs: dict) -> bool:  # noqa: ARG001
    """Consume Lyria config field input. Returns True if consumed."""
    _key = (message.chat.id, message.from_user.id)
    if _key not in _lyria_awaiting_input:
        return False
    wait = _lyria_awaiting_input.pop(_key)
    request_id: str = wait['request_id']
    field: str = wait['field']
    msg_id: int = wait['msg_id']
    d = pending_lyria_configs.get(request_id)
    if d:
        d['cfg'][field] = message.text.strip()
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text=_lyria_cfg_text(request_id),
                reply_markup=_lyria_cfg_keyboard(request_id),
                parse_mode='HTML',
            )
        except Exception:
            await message.bot.send_message(
                chat_id=d['chat_id'],
                text=_lyria_cfg_text(request_id),
                reply_markup=_lyria_cfg_keyboard(request_id),
                parse_mode='HTML',
            )
    return True


async def handle_tts_input(message: types.Message, reply_kwargs: dict) -> bool:  # noqa: ARG001
    """Consume TTS config field input. Returns True if consumed."""
    _tts_key = (message.chat.id, message.from_user.id)
    if _tts_key not in _tts_awaiting_input:
        return False
    wait = _tts_awaiting_input.pop(_tts_key)
    request_id: str = wait['request_id']
    field: str = wait['field']
    msg_id: int = wait['msg_id']
    d = pending_tts_configs.get(request_id)
    if d:
        new_val = message.text.strip()
        if field == 'prompt':
            d['prompt'] = new_val
        elif field in ('scene', 'style', 'pace', 'accent'):
            d['cfg'][field] = new_val
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text=_tts_cfg_text(request_id),
                reply_markup=_tts_cfg_keyboard(request_id),
            )
        except Exception:
            await message.bot.send_message(
                chat_id=d['chat_id'],
                text=_tts_cfg_text(request_id),
                reply_markup=_tts_cfg_keyboard(request_id),
            )
    return True

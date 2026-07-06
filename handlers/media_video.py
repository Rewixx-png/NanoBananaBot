import time
import asyncio
import uuid
import logging

from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile

from state import (
    pending_video_requests,
    user_video_cooldowns,
)
from database import save_pending_gen, delete_pending_gen
from ai_services import (
    start_veo_generation,
    poll_veo_operation,
    generate_video_with_omni,
    explain_generation_error,
)
from utils import check_membership, make_safe_caption
from handlers.common import (
    safe_send,
    _fallback_generation_error_explanation,
    run_progress_bar,
)
from handlers.media_gen import media_router, VEO_MODELS, VIDEO_COOLDOWN

logger = logging.getLogger(__name__)


@media_router.message(Command('video'))
async def cmd_video(message: types.Message):
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member:
        await message.reply('Доступ запрещен.')
        return
    current_time = time.time()
    last_time = user_video_cooldowns.get(message.from_user.id, 0)
    if current_time - last_time < VIDEO_COOLDOWN:
        await message.reply(f'Не спамь блять видосами, подожди еще {int(VIDEO_COOLDOWN - (current_time - last_time))} сек.')
        return
    user_video_cooldowns[message.from_user.id] = current_time
    prompt = (message.text or '').replace('/video', '').strip()
    if message.caption:
        prompt = message.caption.replace('/video', '').strip()
    if not prompt and (not message.photo):
        await message.reply('Напиши промпт после команды, например:\n/video закат над морем\n\nИли прикрепи фото/видео с подписью /video анимируй это.\nOmni Flash поддерживает редактирование видео!')
        return
    image_bytes = None
    video_bytes = None
    if message.photo:
        photo = message.photo[-1]
        try:
            file_info = await message.bot.get_file(photo.file_id)
            downloaded = await message.bot.download_file(file_info.file_path)
            image_bytes = downloaded.read()
        except Exception as e:
            logger.warning(f"Failed to download photo: {e}")
            await message.reply(f"❌ Не удалось скачать фото: {e}")
            return
    if message.video:
        vid = message.video
        try:
            file_info = await message.bot.get_file(vid.file_id)
            downloaded = await message.bot.download_file(file_info.file_path)
            video_bytes = downloaded.read()
        except Exception:
            logger.warning("Video download failed, continuing without video attachment")
            # Don't fail — continue with prompt only
    rows = [[InlineKeyboardButton(text=label, callback_data=f'veosel:{request_id}:{mid}')] for (mid, (label, _)) in VEO_MODELS.items()]
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    reply_kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        reply_kwargs['message_thread_id'] = message.message_thread_id
    await message.reply('Выберите модель Veo для генерации видео:', reply_markup=keyboard, **reply_kwargs)

@media_router.callback_query(F.data.startswith('veosel:'))
async def handle_veo_model_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    (_, request_id, model_id) = parts
    request_data = pending_video_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел. Отправьте /video заново.', show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Только автор запроса может выбирать.', show_alert=True)
        return
    model_info = VEO_MODELS.get(model_id)
    if not model_info:
        await callback.answer('Неизвестная модель.', show_alert=True)
        return
    (model_label, real_model) = model_info
    pending_video_requests.pop(request_id, None)
    await callback.answer()
    message_thread_id = request_data['message_thread_id']
    reply_kwargs = {}
    if message_thread_id:
        reply_kwargs['message_thread_id'] = message_thread_id
    progress_task = None
    state_data = {'status': 'Инициализация...'}
    if callback.message:
        try:
            await callback.message.edit_text('⏳ Запускаю генерацию видео...')
            progress_task = asyncio.create_task(run_progress_bar(callback.bot, request_data['chat_id'], callback.message.message_id, model_label, state_data=state_data))
        except Exception:
            pass
    await callback.bot.send_chat_action(chat_id=request_data['chat_id'], action='upload_video', message_thread_id=message_thread_id)
    gen_id = f'veo_{request_id}'
    try:
        if model_id.startswith('omni'):
            await save_pending_gen(gen_id=gen_id, gen_type='video', user_id=request_data['user_id'], chat_id=request_data['chat_id'], source_message_id=request_data['source_message_id'], message_thread_id=request_data['message_thread_id'], prompt=request_data['prompt'], model='gemini-omni-flash-preview', provider='omni', model_label=model_label)
            (video_bytes, error_msg) = await generate_video_with_omni(
                request_data['prompt'],
                image_bytes=request_data.get('image_bytes'),
                video_bytes=request_data.get('video_bytes'),
                state_data=state_data,
            )
        else:
            (op_name, api_key, start_err) = await start_veo_generation(request_data['prompt'], model=real_model, image_bytes=request_data.get('image_bytes'), state_data=state_data)
            if op_name:
                await save_pending_gen(gen_id=gen_id, gen_type='video', user_id=request_data['user_id'], chat_id=request_data['chat_id'], source_message_id=request_data['source_message_id'], message_thread_id=request_data['message_thread_id'], prompt=request_data['prompt'], model=real_model, provider='veo', veo_operation_name=op_name, veo_api_key=api_key, model_label=model_label)
                (video_bytes, error_msg) = await poll_veo_operation(op_name, api_key, state_data=state_data)
            else:
                (video_bytes, error_msg) = (None, start_err)
    except Exception as e:
        logger.exception(f"Критическая ошибка во время генерации Veo: {e}")
        (video_bytes, error_msg) = (None, f"Внутренняя ошибка сервера: {type(e).__name__}: {e}")
    finally:
        if progress_task:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
        await delete_pending_gen(gen_id)
    if error_msg:
        error_sent_msg = await safe_send(callback.bot.send_message, chat_id=request_data['chat_id'], text=f'❌ Ошибка генерации видео:\n{error_msg}\n\n⏳ Ща спрошу у мозгов, че не так...', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        image_for_explain = request_data.get('image_bytes')
        try:
            explanation = await asyncio.wait_for(explain_generation_error(request_data['prompt'], error_msg, image_bytes=image_for_explain), timeout=30)
        except Exception as e:
            logging.warning(f'Video error explanation failed: {type(e).__name__}: {e}')
            explanation = ''
        if not explanation:
            explanation = _fallback_generation_error_explanation(error_msg)
        if error_sent_msg:
            try:
                await safe_send(callback.bot.edit_message_text, chat_id=request_data['chat_id'], message_id=error_sent_msg.message_id, text=f'❌ Ошибка генерации видео:\n{error_msg}\n\n🧠 Пояснение:\n{explanation}')
            except Exception:
                pass
        return
    if video_bytes:
        video_file = BufferedInputFile(video_bytes, filename='generated.mp4')
        caption = make_safe_caption(f"🎬 Видео ({model_label}) по запросу: ", request_data['prompt'])
        await callback.bot.send_video(chat_id=request_data['chat_id'], video=video_file, caption=caption, reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        from database import add_user_stat, log_prompt
        asyncio.create_task(add_user_stat(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'video'))
        asyncio.create_task(log_prompt(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'video', request_data.get('prompt', '')))
        return
    await callback.bot.send_message(chat_id=request_data['chat_id'], text='❌ Не удалось получить видео.', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)

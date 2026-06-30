import time
import asyncio
import uuid
import logging
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    FULL_ACCESS_CHAT_ID,
    CHAT_ID,
    ALLOWED_USER_IDS,
    TEXT_ONLY_CHAT_ID,
    FULL_ACCESS_CHAT_IMAGE_COOLDOWN,
    IMAGE_COOLDOWN_SECONDS,
    PAYMENT_USERNAME,
    DAILY_GEN_LIMIT,
)
from state import (
    pending_media_groups,
    pending_image_requests,
    pending_prompt_requests,
    full_access_image_cooldowns,
    paid_unlimited_until,
    user_image_cooldowns,
    pending_nsfw_configs,
    chat_custom_limits,
)
from database import save_pending_gen, delete_pending_gen
from ai_services import (
    generate_image_with_gemini,
    generate_image_with_gpt,
    generate_image_with_nvidia,
    generate_image_with_replicate,
    explain_generation_error,
    upscale_image,
    generate_image_prompt,
    fetch_gemini_image_models,
    fetch_openai_image_models,
    fetch_replicate_image_models,
)
from utils import check_membership, make_safe_caption
from handlers.common import (
    safe_send,
    _fallback_generation_error_explanation,
    _track_user,
    run_progress_bar,
    _temp_message,
    _temp_keyboard,
    _providers_keyboard,
    _nsfw_default_cfg,
    _nsfw_cfg_text,
    _nsfw_cfg_keyboard,
    _prompt_ai_keyboard,
    _TEMP_OPTIONS,
    _NSFW_STEPS,
    _NSFW_CFG,
    _NSFW_SIZES,
    _NSFW_DEFAULT_NEG,
    _NSFW_SCHEDULERS,
    _NSFW_CLIP_SKIP,
    _NSFW_PAG,
    _NSFW_RESCALE,
)
from handlers.admin import _check_daily_limit

logger = logging.getLogger(__name__)
media_router = Router()

PROVIDER_MODELS: dict = {
    'gemini': [
        ('Flash 3.1 Image', 'g31flash'),
        ('Flash Lite Image (быстро)', 'g31flashlite'),
        ('Flash 2.0 Image (legacy)', 'g20flash'),
    ],
    'gpt': [('GPT-Image-2', 'gpt2'), ('DALL-E 3', 'dalle3')],
    'flux': [('FLUX Schnell (быстро)', 'schnell'), ('FLUX Dev (качество)', 'fluxdev'), ('FLUX Klein 4B', 'klein')],
    'nsfw': [
        ('Recraft V3 (Дизайн)', 'recraft3'),
        ('FLUX.1 Dev (Реализм)', 'fluxdevr'),
        ('FLUX.1 Schnell (Быстро)', 'fluxschnellr'),
        ('WAI Illustrious v12 🔞', 'wai12'),
        ('WAI Illustrious v11 🔞', 'wai11'),
        ('NSFW FLUX Dev 🔞', 'nsfwflux')
    ]
}

MODEL_TO_REAL: dict = {
    'g31flash': ('gemini', 'gemini-3.1-flash-image-preview'),
    'g31flashlite': ('gemini', 'gemini-3.1-flash-lite-image-preview'),
    'g20flash': ('gemini', 'gemini-2.0-flash-preview-image-generation'),
    'gpt2': ('gpt', 'gpt-image-2'),
    'dalle3': ('gpt', 'dall-e-3'),
    'schnell': ('flux', 'black-forest-labs/flux.1-schnell'),
    'fluxdev': ('flux', 'black-forest-labs/flux.1-dev'),
    'klein': ('flux', 'black-forest-labs/flux_2-klein-4b'),
    'recraft3': ('nsfw', 'recraft-ai/recraft-v3'),
    'fluxdevr': ('nsfw', 'black-forest-labs/flux-dev'),
    'fluxschnellr': ('nsfw', 'black-forest-labs/flux-schnell'),
    'wai12': ('nsfw', 'aisha-ai-official/wai-nsfw-illustrious-v12'),
    'wai11': ('nsfw', 'aisha-ai-official/wai-nsfw-illustrious-v11'),
    'nsfwflux': ('nsfw', 'aisha-ai-official/nsfw-flux-dev')
}

VEO_MODELS: dict = {
    'veo1': ('Veo 3.1 Fast', 'veo-3.1-fast-generate-preview'),
    'veo2': ('Veo 3.1', 'veo-3.1-generate-preview'),
    'veo3': ('Veo 3.1 Lite', 'veo-3.1-lite-generate-preview'),
    'veo0': ('Veo 2 (deprecated 30.06)', 'veo-2.0-generate-001'),
}

VIDEO_COOLDOWN = 60

TTS_MODELS: dict = {
    'tts0': ('Gemini 3.5 Flash TTS', 'gemini-3.5-flash-tts'),
    'tts1': ('Gemini 3.1 Flash TTS', 'gemini-3.1-flash-tts-preview'),
}

_TTS_LANGS = [('🇷🇺 RU', 'ru-RU'), ('🇬🇧 EN', 'en-US'), ('🇯🇵 JA', 'ja-JP')]
_TTS_TEMPS = [0.1, 0.5, 1.0, 1.5, 2.0]
TTS_VOICES = ['Puck', 'Charon', 'Kore', 'Fenrir', 'Leda', 'Orus', 'Aoede', 'Callirrhoe', 'Autonoe', 'Enceladus', 'Iapetus', 'Umbriel', 'Algieba', 'Despina', 'Erinome', 'Algenib', 'Rasalgethi', 'Laomedeia', 'Achernar', 'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird', 'Zubenelgenubi', 'Vindemiatrix', 'Sadachbia', 'Sadaltager', 'Sulafat']

_nsfw_awaiting_input: dict = {}

async def refresh_models():
    gemini_models = await fetch_gemini_image_models()
    if gemini_models:
        PROVIDER_MODELS['gemini'] = [(label, f'gi{i}') for (i, (label, _)) in enumerate(gemini_models)]
        for (i, (_, model_id)) in enumerate(gemini_models):
            MODEL_TO_REAL[f'gi{i}'] = ('gemini', model_id)
    openai_models = await fetch_openai_image_models()
    if openai_models:
        PROVIDER_MODELS['gpt'] = [(label, f'oi{i}') for (i, (label, _)) in enumerate(openai_models)]
        for (i, (_, model_id)) in enumerate(openai_models):
            MODEL_TO_REAL[f'oi{i}'] = ('gpt', model_id)
    replicate_models = await fetch_replicate_image_models()
    if replicate_models:
        custom_models = [
            ('Recraft V3 (Дизайн)', 'recraft-ai/recraft-v3'),
            ('FLUX.1 Dev (Реализм)', 'black-forest-labs/flux-dev'),
            ('FLUX.1 Schnell (Быстро)', 'black-forest-labs/flux-schnell'),
            ('WAI Illustrious v12 🔞', 'aisha-ai-official/wai-nsfw-illustrious-v12'),
            ('WAI Illustrious v11 🔞', 'aisha-ai-official/wai-nsfw-illustrious-v11'),
            ('NSFW FLUX Dev 🔞', 'aisha-ai-official/nsfw-flux-dev')
        ]
        all_models = custom_models.copy()
        custom_paths = {p for (_, p) in custom_models}
        for (label, path) in replicate_models:
            if path not in custom_paths:
                all_models.append((label, path))
        all_models = all_models[:25]
        PROVIDER_MODELS['nsfw'] = [(label, f'rep{i}') for (i, (label, _)) in enumerate(all_models)]
        for (i, (_, model_id)) in enumerate(all_models):
            MODEL_TO_REAL[f'rep{i}'] = ('nsfw', model_id)
    from ai_services import fetch_veo_models, fetch_gemini_tts_models
    veo_models = await fetch_veo_models()
    if veo_models:
        for (i, (label, model_id)) in enumerate(veo_models):
            VEO_MODELS[f'veo{i}'] = (label, model_id)
    tts_models = await fetch_gemini_tts_models()
    if tts_models:
        for (i, (label, model_id)) in enumerate(tts_models):
            TTS_MODELS[f'tts{i}'] = (label, model_id)
    logger.info(f"Models refreshed: Gemini={len(PROVIDER_MODELS['gemini'])} GPT={len(PROVIDER_MODELS['gpt'])} Veo={len(VEO_MODELS)} TTS={len(TTS_MODELS)}")

@media_router.message(Command('image'))
async def cmd_image(message: types.Message):
    _track_user(message)
    if message.media_group_id:
        if message.media_group_id in pending_media_groups:
            photo = message.photo[-1] if message.photo else None
            if photo:
                try:
                    file_info = await message.bot.get_file(photo.file_id)
                    downloaded_file = await message.bot.download_file(file_info.file_path)
                    group = pending_media_groups[message.media_group_id]
                    group['images'].append(downloaded_file.read())
                    group.setdefault('file_ids', []).append(photo.file_id)
                except Exception as e:
                    logger.warning(f"Failed to download subsequent album photo in cmd_image: {e}")
            return
        else:
            pending_media_groups[message.media_group_id] = {'images': [], 'file_ids': [], 'request_id': None}
    if message.chat.type == 'private':
        from utils import is_banned
        if is_banned(message.from_user.id):
            return
    else:
        is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
        if not is_member:
            await message.reply('Доступ запрещен. Вы не состоите в обязательной беседе.')
            return
    current_time = time.time()
    uid = message.from_user.id
    if uid not in ALLOWED_USER_IDS:
        if message.chat.id == FULL_ACCESS_CHAT_ID:
            is_main_member = True
            try:
                m = await message.bot.get_chat_member(chat_id=CHAT_ID, user_id=uid)
                is_main_member = m.status in ('member', 'administrator', 'creator', 'restricted')
            except Exception as e:
                logger.warning(f'is_main_member check failed for {uid}: {e}')
            if not is_main_member and current_time >= paid_unlimited_until.get(uid, 0):
                last_fa = full_access_image_cooldowns.get(uid, 0)
                remaining = FULL_ACCESS_CHAT_IMAGE_COOLDOWN - (current_time - last_fa)
                if remaining > 0:
                    secs = int(remaining)
                    await message.reply(f'Не спамь блять картинками, подожди ещё {secs} сек.')
                    return
            full_access_image_cooldowns[uid] = current_time
        else:
            last_time = user_image_cooldowns.get(uid, 0)
            if current_time - last_time < IMAGE_COOLDOWN_SECONDS:
                await message.reply(f'Не спамь блять картинками, подожди еще {int(IMAGE_COOLDOWN_SECONDS - (current_time - last_time))} сек.')
                return
            user_image_cooldowns[uid] = current_time
    prompt = message.text.replace('/image', '').strip() if message.text else ''
    if message.caption:
        prompt = message.caption.replace('/image', '').strip()
    if not prompt and (not message.photo):
        await message.reply('Напишите промпт после команды, например:\n/image красивый закат')
        return
    images_bytes = []
    file_ids = []
    if message.photo:
        if message.media_group_id:
            group = pending_media_groups[message.media_group_id]
            images_bytes = group['images']
            file_ids = group['file_ids']
        photo = message.photo[-1]
        file_ids.append(photo.file_id)
        file_info = await message.bot.get_file(photo.file_id)
        downloaded_file = await message.bot.download_file(file_info.file_path)
        images_bytes.append(downloaded_file.read())
        if message.media_group_id:
            await asyncio.sleep(2.5)
            group = pending_media_groups.pop(message.media_group_id, None)
            if group:
                images_bytes = group['images']
                file_ids = group.get('file_ids', file_ids)
    request_id = uuid.uuid4().hex[:10]
    thread_id = message.message_thread_id if message.chat.is_forum else None
    pending_image_requests[request_id] = {'user_id': message.from_user.id, 'first_name': message.from_user.first_name or 'Аноним', 'username': message.from_user.username or '', 'chat_id': message.chat.id, 'source_message_id': message.message_id, 'message_thread_id': thread_id, 'prompt': prompt, 'image_bytes': images_bytes[0] if len(images_bytes) == 1 else None, 'images_bytes': images_bytes if len(images_bytes) > 1 else None, 'file_ids': file_ids}
    reply_kwargs = {}
    if message.chat.is_forum and thread_id:
        reply_kwargs['message_thread_id'] = thread_id
    if images_bytes and prompt:
        pending_prompt_requests[request_id] = {'user_id': message.from_user.id, 'chat_id': message.chat.id, 'source_message_id': message.message_id, 'message_thread_id': thread_id, 'prompt': prompt, 'images_bytes': images_bytes, 'file_ids': file_ids, 'prev_prompts': [], 'current_ai_prompt': None}
        photo_word = 'фотки' if len(images_bytes) > 1 else 'фотку'
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🤖 Использовать шаблон промта', callback_data=f'pask:{request_id}')], [InlineKeyboardButton(text='⚡ Генерировать с моим промтом', callback_data=f'pbase:{request_id}')]])
        await message.reply(f"Слышь, {photo_word} вижу. Твой промт — ну такое. Могу через Gemini Pro сделать нормальный промт по {('этим' if len(images_bytes) > 1 else 'этому')} {('фоткам' if len(images_bytes) > 1 else 'фото')} и твоей идее. Жмякай кнопку или генерируй со своим мусором.", reply_markup=keyboard, **reply_kwargs)
        return
    await message.reply('Через какую модель хотите сгенерировать фото?', reply_markup=_providers_keyboard(request_id, message.chat.id, len(images_bytes)), **reply_kwargs)

@media_router.callback_query(F.data.startswith('ptmp:'))
async def handle_temp_select(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 3:
        return
    (_, request_id, idx_str) = parts
    req = pending_image_requests.get(request_id)
    if not req:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != req['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    idx = int(idx_str)
    (temp_val, temp_label, _) = _TEMP_OPTIONS[idx]
    req['temperature'] = temp_val
    await callback.answer(f'Температура: {temp_label} ({temp_val})')
    if req.get('selected_model'):
        pending_image_requests.pop(request_id, None)
        real_model = req['selected_model']
        selected_label = req.get('selected_label', real_model)
        message_thread_id = req.get('message_thread_id')
        reply_kwargs = {'message_thread_id': message_thread_id} if message_thread_id else {}
        await callback.bot.send_chat_action(chat_id=req['chat_id'], action='upload_photo', message_thread_id=message_thread_id)
        progress_task = None
        state_data = {'status': 'Инициализация...'}
        try:
            await callback.message.edit_text(f'🌡️ {temp_label} ({temp_val})\n⏳ Запускаю генерацию...')
            progress_task = asyncio.create_task(run_progress_bar(callback.bot, req['chat_id'], callback.message.message_id, selected_label, state_data=state_data))
        except Exception:
            pass
        imgs = req.get('images_bytes') or ([req['image_bytes']] if req.get('image_bytes') else None)
        gen_id = f'img_{request_id}'
        try:
            await save_pending_gen(gen_id=gen_id, gen_type='image', user_id=req['user_id'], chat_id=req['chat_id'], source_message_id=req['source_message_id'], message_thread_id=message_thread_id, prompt=req['prompt'], model=real_model, provider='gemini', file_ids=req.get('file_ids', []), model_label=selected_label)
            (result_img, error_msg) = await generate_image_with_gemini(req['prompt'], images_bytes=imgs, model=real_model, temperature=temp_val, state_data=state_data)
        except Exception as e:
            logger.exception(f"Критическая ошибка во время генерации Gemini: {e}")
            (result_img, error_msg) = (None, f"Внутренняя ошибка сервера: {type(e).__name__}: {e}")
        finally:
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
            await delete_pending_gen(gen_id)
        await _send_generation_result(callback.bot, req, request_id, result_img, error_msg, selected_label, imgs, reply_kwargs)
        return
    imgs = req.get('images_bytes') or ([req['image_bytes']] if req.get('image_bytes') else [])
    try:
        await callback.message.edit_text(f'🌡️ {temp_label} ({temp_val}) — выбрано\n\nЧерез какую модель генерировать?', reply_markup=_providers_keyboard(request_id, req['chat_id'], len(imgs)))
    except Exception:
        pass

async def _send_generation_result(bot, request_data, request_id, result_img, error_msg, model_label, imgs, reply_kwargs):
    if error_msg:
        err_msg = await safe_send(bot.send_message, chat_id=request_data['chat_id'], text=f'⏳ Ошибка генерации, анализирую...', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        first_image = imgs[0] if imgs else None
        try:
            explanation = await asyncio.wait_for(explain_generation_error(request_data['prompt'] or '', error_msg, image_bytes=first_image), timeout=30)
        except Exception as e:
            logging.warning(f'Image error explanation failed: {type(e).__name__}: {e}')
            explanation = ''
        if not explanation or 'Ебать, гугл зацензурил' in explanation:
            explanation = _fallback_generation_error_explanation(error_msg)
        if err_msg:
            try:
                import html as _html_mod
                safe_err = _html_mod.escape(error_msg)
                safe_exp = _html_mod.escape(explanation)
                from aiogram.types import InputRichMessage as _IRM_err, ReplyParameters as _RP_err
                rich_err = (
                    f'<details><summary>❌ Ошибка генерации</summary>'
                    f'<b>Полная ошибка от API:</b>\n<pre>{safe_err}</pre>\n\n'
                    f'<b>Пояснение:</b>\n{safe_exp}'
                    f'</details>'
                )
                await bot.send_rich_message(
                    chat_id=request_data['chat_id'],
                    rich_message=_IRM_err(html=rich_err),
                    reply_parameters=_RP_err(message_id=request_data['source_message_id']),
                )
                await bot.delete_message(chat_id=request_data['chat_id'], message_id=err_msg.message_id)
            except Exception:
                await safe_send(bot.edit_message_text, chat_id=request_data['chat_id'], message_id=err_msg.message_id, text=f'❌ Ошибка:\n{error_msg}\n\n🧠 Пояснение:\n{explanation}')
        return
    if result_img:
        from handlers.common import _remember_generated_draw_message
        caption = make_safe_caption(f"🎨 Ваш результат ({model_label}) по запросу: ", request_data['prompt']) if request_data['prompt'] else f'🎨 Ваш результат ({model_label}) готов.'
        sent_photo = await safe_send(bot.send_photo, chat_id=request_data['chat_id'], photo=BufferedInputFile(result_img, filename='generated.jpg'), caption=caption, reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        if sent_photo:
            _remember_generated_draw_message(request_data['chat_id'], sent_photo.message_id, result_img, request_data.get('prompt', ''), request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'))
        upscale_msg = await safe_send(bot.send_message, chat_id=request_data['chat_id'], text='⬆️ Улучшаю качество через AI upscaler...', **reply_kwargs)
        (upscaled, up_err) = await upscale_image(result_img)
        try:
            await bot.delete_message(chat_id=request_data['chat_id'], message_id=upscale_msg.message_id)
        except Exception:
            pass
        if upscaled:
            await safe_send(bot.send_document, chat_id=request_data['chat_id'], document=BufferedInputFile(upscaled, filename='upscaled.png'), caption=f'✨ Улучшенная версия ({model_label}) 2x — без сжатия', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
        from database import add_user_stat, log_prompt
        asyncio.create_task(add_user_stat(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'image'))
        asyncio.create_task(log_prompt(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'image', request_data.get('prompt', '')))
        return
    await bot.send_message(chat_id=request_data['chat_id'], text='❌ Не удалось получить изображение.', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)

@media_router.callback_query(F.data.startswith('nsfwinput:'))
async def handle_nsfw_input(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 3:
        return
    (_, request_id, field) = parts
    d = pending_nsfw_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await callback.answer()
    field_name = {'prompt': 'промпт', 'neg': 'негативный промпт', 'seed': 'seed (число, -1 = случайный)'}.get(field, field)
    _nsfw_awaiting_input[d['chat_id'], d['user_id']] = {'request_id': request_id, 'field': field, 'msg_id': callback.message.message_id}
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data=f'nsfwcancel:{request_id}')]])
    try:
        await callback.message.edit_text(f'✏️ Напиши {field_name} следующим сообщением:\n\n(просто отправь текст в чат)', reply_markup=cancel_kb)
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('nsfwcancel:'))
async def handle_nsfw_cancel(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_nsfw_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
    _nsfw_awaiting_input.pop((d['chat_id'], d['user_id']), None)
    await callback.answer('Отменено')
    try:
        await callback.message.edit_text(_nsfw_cfg_text(request_id), reply_markup=_nsfw_cfg_keyboard(request_id))
    except Exception:
        pass

@media_router.callback_query(F.data == 'noop')
async def handle_noop(callback: types.CallbackQuery):
    await callback.answer()

@media_router.callback_query(F.data.startswith('nsfwcfg:'))
async def handle_nsfw_config(callback: types.CallbackQuery):
    parts = callback.data.split(':', 3)
    if len(parts) != 4:
        await callback.answer()
        return
    (_, request_id, field, value) = parts
    d = pending_nsfw_configs.get(request_id)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    if field == 'steps':
        d['cfg']['steps'] = int(value)
    elif field == 'cfg':
        d['cfg']['cfg'] = float(value)
    elif field == 'size':
        d['cfg']['size'] = value
    elif field == 'scheduler':
        d['cfg']['scheduler'] = value
    elif field == 'clip_skip':
        d['cfg']['clip_skip'] = int(value)
    elif field == 'pag_scale':
        d['cfg']['pag_scale'] = float(value)
    elif field == 'rescale':
        d['cfg']['rescale'] = float(value)
    elif field == 'prepend':
        d['cfg']['prepend'] = value == '1'
    elif field == 'batch':
        d['cfg']['batch'] = int(value)
    await callback.answer()
    try:
        await callback.message.edit_text(_nsfw_cfg_text(request_id), reply_markup=_nsfw_cfg_keyboard(request_id))
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('nsfwgen:'))
async def handle_nsfw_generate(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    d = pending_nsfw_configs.pop(request_id, None)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
    await callback.answer()
    message_thread_id = d['message_thread_id']
    reply_kwargs = {'message_thread_id': message_thread_id} if message_thread_id else {}
    await callback.bot.send_chat_action(chat_id=d['chat_id'], action='upload_photo', message_thread_id=message_thread_id)
    progress_task = None
    state_data = {'status': 'Инициализация...'}
    try:
        await callback.message.edit_text(f"⏳ Генерирую через {d['label']}...")
        progress_task = asyncio.create_task(run_progress_bar(callback.bot, d['chat_id'], callback.message.message_id, d['label'], state_data=state_data))
    except Exception:
        pass
    try:
        (results, error_msg) = await generate_image_with_replicate(d['prompt'], model=d['model'], state_data=state_data)
    except Exception as e:
        logger.exception(f"Критическая ошибка во время генерации NSFW: {e}")
        (results, error_msg) = (None, f"Внутренняя ошибка сервера: {type(e).__name__}: {e}")
    finally:
        if progress_task:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
    if error_msg or not results:
        await _send_generation_result(callback.bot, d, request_id, None, error_msg or 'Нет результата', d['label'], None, reply_kwargs)
        return
    if len(results) == 1:
        await _send_generation_result(callback.bot, d, request_id, results[0], None, d['label'], None, reply_kwargs)
    else:
        from aiogram.types import InputMediaPhoto
        caption = f"🎨 {d['label']} × {len(results)}\n{d['prompt'][:100]}" if d.get('prompt') else f"🎨 {d['label']} × {len(results)}"
        media = [InputMediaPhoto(media=BufferedInputFile(img, filename=f'nsfw_{i + 1}.jpg'), caption=caption if i == 0 else None) for (i, img) in enumerate(results)]
        await callback.bot.send_media_group(chat_id=d['chat_id'], media=media, reply_to_message_id=d['source_message_id'], **reply_kwargs)
        upscale_msg = await callback.bot.send_message(chat_id=d['chat_id'], text='⬆️ Улучшаю первое фото...', **reply_kwargs)
        (upscaled, _) = await upscale_image(results[0])
        try:
            await callback.bot.delete_message(chat_id=d['chat_id'], message_id=upscale_msg.message_id)
        except Exception:
            pass
        if upscaled:
            await callback.bot.send_document(chat_id=d['chat_id'], document=BufferedInputFile(upscaled, filename='upscaled.png'), caption=f'✨ #{1} улучшенная версия 2x', reply_to_message_id=d['source_message_id'], **reply_kwargs)

async def _run_prompt_generation(callback: types.CallbackQuery, request_id: str):
    data = pending_prompt_requests.get(request_id)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_text('🧠 Gemini Pro анализирует фото и промт...')
    except Exception:
        pass
    (eng, rus, err) = await generate_image_prompt(data['prompt'], data['images_bytes'], data['prev_prompts'])
    if not eng:
        try:
            await callback.message.edit_text(f'❌ Ошибка генерации промта: {err}')
        except Exception:
            pass
        return
    data['current_ai_prompt'] = eng
    data['prev_prompts'].append(eng)
    rus_line = f'\n\n🇷🇺 По-русски:\n{rus}' if rus else ''
    text = f'🤖 AI-промт готов:\n\n🇬🇧 English:\n<code>{eng}</code>{rus_line}'
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=_prompt_ai_keyboard(request_id))
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('pask:'))
async def handle_prompt_ask(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    data = pending_prompt_requests.get(request_id)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await _run_prompt_generation(callback, request_id)

@media_router.callback_query(F.data.startswith('pother:'))
async def handle_prompt_other(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    data = pending_prompt_requests.get(request_id)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await _run_prompt_generation(callback, request_id)

@media_router.callback_query(F.data.startswith('puse:'))
async def handle_prompt_use(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    data = pending_prompt_requests.pop(request_id, None)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    chosen_prompt = data.get('current_ai_prompt') or data['prompt']
    req = pending_image_requests.get(request_id, {})
    req['prompt'] = chosen_prompt
    pending_image_requests[request_id] = req
    await callback.answer()
    req = pending_image_requests.get(request_id, {})
    try:
        await callback.message.edit_text('Через какую модель генерировать?', reply_markup=_providers_keyboard(request_id, req.get('chat_id', data['chat_id']), len(data.get('images_bytes') or [])))
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('pbase:'))
async def handle_prompt_base(callback: types.CallbackQuery):
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    data = pending_prompt_requests.pop(request_id, None)
    if not data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await callback.answer()
    req = pending_image_requests.get(request_id, {})
    try:
        await callback.message.edit_text('Через какую модель генерировать?', reply_markup=_providers_keyboard(request_id, req.get('chat_id', data['chat_id']), len(data.get('images_bytes') or [])))
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('imgprov:'))
async def handle_provider_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    (_, request_id, provider) = parts
    request_data = pending_image_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел. Отправьте /image заново.', show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Только автор запроса может выбирать.', show_alert=True)
        return
    if provider == 'gpt' and callback.message and (callback.message.chat.id == TEXT_ONLY_CHAT_ID):
        await callback.answer('GPT недоступен в этой беседе.', show_alert=True)
        return
    uid = callback.from_user.id
    if provider in ('gemini', 'gpt') and uid not in ALLOWED_USER_IDS:
        is_main_member = False
        try:
            m = await callback.bot.get_chat_member(chat_id=CHAT_ID, user_id=uid)
            is_main_member = m.status in ('member', 'administrator', 'creator', 'restricted')
        except Exception:
            is_main_member = True
        if not is_main_member:
            (allowed, remaining) = _check_daily_limit(uid, callback.message.chat.id if callback.message else request_data['chat_id'])
            if not allowed:
                (req_limit, days) = chat_custom_limits.get(callback.message.chat.id if callback.message else request_data['chat_id'], (DAILY_GEN_LIMIT, 1))
                await callback.answer(f'❌ Лимит {req_limit} генерации за {days} дн. исчерпан.\nДля безлимита свяжитесь с {PAYMENT_USERNAME} и пополните карту Тбанка на 10₽.', show_alert=True)
                return
    models = PROVIDER_MODELS.get(provider, [])
    if not models:
        await callback.answer('Неизвестный провайдер.', show_alert=True)
        return
    rows = []
    for (label, mid) in models:
        rows.append([InlineKeyboardButton(text=label, callback_data=f'imgsel:{request_id}:{mid}')])
    rows.append([InlineKeyboardButton(text='← Назад', callback_data=f'imgback:{request_id}')])
    provider_names = {'gemini': 'Gemini', 'gpt': 'GPT', 'flux': 'FLUX (NVIDIA)', 'nsfw': 'Replicate'}
    await callback.answer()
    try:
        await callback.message.edit_text(f'Выберите модель {provider_names.get(provider, provider)}:', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('imgback:'))
async def handle_provider_back(callback: types.CallbackQuery):
    if not callback.data:
        return
    parts = callback.data.split(':')
    if len(parts) != 2:
        return
    (_, request_id) = parts
    request_data = pending_image_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Только автор запроса.', show_alert=True)
        return
    await callback.answer()
    keyboard = _providers_keyboard(request_id, request_data['chat_id'], len(request_data.get('images_bytes') or []))
    try:
        await callback.message.edit_text('Через какую модель хотите сгенерировать фото?', reply_markup=keyboard)
    except Exception:
        pass

@media_router.callback_query(F.data.startswith('imgsel:'))
async def handle_image_model_select(callback: types.CallbackQuery):
    if not callback.data:
        await callback.answer('Некорректные данные кнопки.', show_alert=True)
        return
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer('Некорректные данные кнопки.', show_alert=True)
        return
    (_, request_id, model_id) = parts
    request_data = pending_image_requests.get(request_id)
    if not request_data:
        await callback.answer('Запрос устарел. Отправьте /image заново.', show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return
    if callback.from_user.id != request_data['user_id']:
        await callback.answer('Эту кнопку может нажать только тот, кто отправил /image.', show_alert=True)
        return
    model_info = MODEL_TO_REAL.get(model_id)
    if not model_info:
        await callback.answer('Неизвестная модель.', show_alert=True)
        return
    (provider, real_model) = model_info
    selected_label = next((l for lst in PROVIDER_MODELS.values() for (l, m) in lst if next((k for (k, (p, rm)) in MODEL_TO_REAL.items() if rm == m and p == provider), None) == model_id), real_model)
    await callback.answer()
    if provider == 'gemini':
        request_data['selected_model'] = real_model
        request_data['selected_provider'] = provider
        request_data['selected_label'] = selected_label
        try:
            await callback.message.edit_text(_temp_message(), reply_markup=_temp_keyboard(request_id))
        except Exception:
            pass
        return
    if provider == 'nsfw':
        pending_image_requests.pop(request_id, None)
        imgs = request_data.get('images_bytes') or ([request_data['image_bytes']] if request_data.get('image_bytes') else None)
        pending_nsfw_configs[request_id] = {'user_id': request_data['user_id'], 'chat_id': request_data['chat_id'], 'source_message_id': request_data['source_message_id'], 'message_thread_id': request_data['message_thread_id'], 'prompt': request_data['prompt'], 'model': real_model, 'label': selected_label, 'image_bytes': imgs[0] if imgs else None, 'cfg': _nsfw_default_cfg(real_model)}
        try:
            await callback.message.edit_text(_nsfw_cfg_text(request_id), reply_markup=_nsfw_cfg_keyboard(request_id))
        except Exception:
            pass
        return
    pending_image_requests.pop(request_id, None)
    message_thread_id = request_data['message_thread_id']
    reply_kwargs = {}
    if message_thread_id:
        reply_kwargs['message_thread_id'] = message_thread_id
    await callback.bot.send_chat_action(chat_id=request_data['chat_id'], action='upload_photo', message_thread_id=message_thread_id)
    progress_task = None
    state_data = {'status': 'Инициализация...'}
    if callback.message:
        try:
            await callback.message.edit_text('⏳ Запускаю генерацию...')
            progress_task = asyncio.create_task(run_progress_bar(callback.bot, request_data['chat_id'], callback.message.message_id, selected_label, state_data=state_data))
        except Exception:
            pass
    imgs = request_data.get('images_bytes') or ([request_data['image_bytes']] if request_data.get('image_bytes') else None)
    gen_id = f'img_{request_id}'
    try:
        await save_pending_gen(gen_id=gen_id, gen_type='image', user_id=request_data['user_id'], chat_id=request_data['chat_id'], source_message_id=request_data['source_message_id'], message_thread_id=request_data['message_thread_id'], prompt=request_data['prompt'], model=real_model, provider=provider, file_ids=request_data.get('file_ids', []), model_label=selected_label)
        if provider == 'gpt':
            (result_img, error_msg) = await generate_image_with_gpt(request_data['prompt'], images_bytes=imgs, model=real_model, state_data=state_data)
        elif provider == 'flux':
            (result_img, error_msg) = await generate_image_with_nvidia(request_data['prompt'], model=real_model, state_data=state_data)
        elif provider == 'nsfw':
            (result_img, error_msg) = await generate_image_with_replicate(request_data['prompt'], model=real_model, state_data=state_data)
        else:
            (result_img, error_msg) = await generate_image_with_gemini(request_data['prompt'], images_bytes=imgs, model=real_model, temperature=request_data.get('temperature', 1.0), state_data=state_data)
    except Exception as e:
        logger.exception(f"Критическая ошибка во время генерации изображения: {e}")
        (result_img, error_msg) = (None, f"Внутренняя ошибка сервера: {type(e).__name__}: {e}")
    finally:
        if progress_task:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
        await delete_pending_gen(gen_id)
    await _send_generation_result(callback.bot, request_data, request_id, result_img, error_msg, selected_label, imgs, reply_kwargs)

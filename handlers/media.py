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
    pending_video_requests,
    user_video_cooldowns,
    pending_tts_requests,
    pending_tts_configs,
    tts_voice_previews,
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
    fetch_veo_models,
    fetch_gemini_tts_models,
    start_veo_generation,
    poll_veo_operation,
    generate_tts_with_gemini,
)
from utils import check_membership, make_safe_caption
from handlers.common import (
    safe_send,
    _fallback_generation_error_explanation,
    _track_user,
    run_progress_bar,
)
from handlers.admin import _check_daily_limit

logger = logging.getLogger(__name__)
media_router = Router()

_TEMP_OPTIONS = [
    (0.1, '🎯 Точный', 'строго следует промпту, почти без вариаций'),
    (0.5, '⚖️ Умеренный', 'баланс точности и разнообразия'),
    (1.0, '✨ Стандарт', 'стандартная генерация (по умолчанию)'),
    (1.5, '🎨 Творческий', 'больше вариативности и интерпретации'),
    (2.0, '🌀 Безумный', 'максимальная непредсказуемость'),
]

_NSFW_STEPS = [20, 25, 28, 35, 50]
_NSFW_CFG = [5.0, 6.5, 7.0, 8.5, 10.0]
_NSFW_SIZES = ['512x768', '768x1024', '896x1152', '1024x1024', '1024x1536']
_NSFW_DEFAULT_NEG = 'lowres, bad anatomy, bad hands, text, error, missing fingers, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, blurry'

PROVIDER_MODELS: dict = {
    'gemini': [('Flash 3.1 Image', 'g31flash'), ('Flash 2.0 Image', 'g20flash')],
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
    'veo0': ('Veo 2', 'veo-2.0-generate-001'),
    'veo1': ('Veo 3.1 Fast', 'veo-3.1-fast-generate-preview'),
    'veo2': ('Veo 3.1', 'veo-3.1-generate-preview'),
    'veo3': ('Veo 3.1 Lite', 'veo-3.1-lite-generate-preview'),
}

VIDEO_COOLDOWN = 60

TTS_MODELS: dict = {'tts0': ('Gemini Flash TTS Preview', 'gemini-3.1-flash-tts-preview')}

_TTS_LANGS = [('🇷🇺 RU', 'ru-RU'), ('🇬🇧 EN', 'en-US'), ('🇯🇵 JA', 'ja-JP')]
_TTS_TEMPS = [0.1, 0.5, 1.0, 1.5, 2.0]
TTS_VOICES = ['Puck', 'Charon', 'Kore', 'Fenrir', 'Leda', 'Orus', 'Aoede', 'Callirrhoe', 'Autonoe', 'Enceladus', 'Iapetus', 'Umbriel', 'Algieba', 'Despina', 'Erinome', 'Algenib', 'Rasalgethi', 'Laomedeia', 'Achernar', 'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird', 'Zubenelgenubi', 'Vindemiatrix', 'Sadachbia', 'Sadaltager', 'Sulafat']

_tts_awaiting_input: dict = {}
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
    veo_models = await fetch_veo_models()
    if veo_models:
        for (i, (label, model_id)) in enumerate(veo_models):
            VEO_MODELS[f'veo{i}'] = (label, model_id)
    tts_models = await fetch_gemini_tts_models()
    if tts_models:
        for (i, (label, model_id)) in enumerate(tts_models):
            TTS_MODELS[f'tts{i}'] = (label, model_id)
    logger.info(f"Models refreshed: Gemini={len(PROVIDER_MODELS['gemini'])} GPT={len(PROVIDER_MODELS['gpt'])} Veo={len(VEO_MODELS)} TTS={len(TTS_MODELS)}")

def _temp_message() -> str:
    lines = ['🌡️ Выберите температуру генерации:\n', 'Температура влияет на то, насколько точно ИИ следует промпту.', 'Диапазон: 0.1 (точно) — 2.0 (творческий хаос)\n']
    for (val, label, desc) in _TEMP_OPTIONS:
        lines.append(f'{label} — {desc}')
    return '\n'.join(lines)

def _temp_keyboard(request_id: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f'{label} ({val})', callback_data=f'ptmp:{request_id}:{i}')] for (i, (val, label, _)) in enumerate(_TEMP_OPTIONS)]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _providers_keyboard(request_id: str, chat_id: int, photo_count: int=0) -> InlineKeyboardMarkup:
    providers = ['gemini', 'flux', 'nsfw'] if chat_id == TEXT_ONLY_CHAT_ID else ['gemini', 'gpt', 'flux', 'nsfw']
    labels = {'gemini': 'Gemini', 'gpt': 'GPT', 'flux': 'FLUX', 'nsfw': 'Replicate'}
    photo_label = f' 📎{photo_count} фото' if photo_count > 1 else ''
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=labels[p] + (photo_label if p not in ('flux', 'nsfw') else ''), callback_data=f'imgprov:{request_id}:{p}') for p in providers]])

def _nsfw_default_cfg(model: str) -> dict:
    if 'flux' in model:
        return {'steps': 28, 'cfg': 3.5, 'size': '1024x1024', 'neg': ''}
    return {'steps': 28, 'cfg': 7.0, 'size': '896x1152', 'neg': _NSFW_DEFAULT_NEG, 'scheduler': 'DPM++ 2M Karras', 'clip_skip': 2, 'pag_scale': 0, 'rescale': 1.0, 'prepend': True, 'seed': -1, 'batch': 1}

def _nsfw_cfg_text(request_id: str) -> str:
    d = pending_nsfw_configs.get(request_id, {})
    cfg = d.get('cfg', {})
    prompt = d.get('prompt', '')[:100]
    neg_full = cfg.get('neg', '')
    neg = neg_full[:150]
    label = d.get('label', 'NSFW')
    neg_display = f'''"{neg}{('...' if len(neg_full) > 150 else '')}"''' if neg_full else 'не задан (стандартный)'
    is_wai = 'flux' not in d.get('model', '')
    extra = ''
    if is_wai:
        extra = f"\n\nКол-во: {cfg.get('batch', 1)} шт  |  Планировщик: {cfg.get('scheduler', 'DPM++ 2M Karras')}\nCLIP Skip: {cfg.get('clip_skip', 2)}  |  PAG: {cfg.get('pag_scale', 0)}  |  Rescale: {cfg.get('rescale', 1.0)}\nПрепромпт: {('✅' if cfg.get('prepend', True) else '❌')}  |  Seed: {cfg.get('seed', -1)}"
    return f'''⚙️ {label}\n\n📝 Промпт:\n"{prompt}"\n\n🚫 Негативный промпт:\n{neg_display}\n\nШаги: {cfg.get('steps', 28)}  |  CFG: {cfg.get('cfg', 7.0)}  |  Размер: {cfg.get('size', '896x1152')}{extra}'''

def _nsfw_cfg_keyboard(request_id: str) -> InlineKeyboardMarkup:
    d = pending_nsfw_configs.get(request_id, {})
    cfg = d.get('cfg', {})
    cur_steps = cfg.get('steps', 28)
    cur_cfgv = cfg.get('cfg', 7.0)
    cur_size = cfg.get('size', '896x1152')
    cur_sched = cfg.get('scheduler', 'DPM++ 2M Karras')
    cur_clip = cfg.get('clip_skip', 2)
    cur_pag = cfg.get('pag_scale', 0)
    cur_resc = cfg.get('rescale', 1.0)
    cur_pre = cfg.get('prepend', True)
    cur_seed = cfg.get('seed', -1)
    is_wai = 'flux' not in d.get('model', '')

    def row(field, options, current):
        return [InlineKeyboardButton(text=f"{('✅' if str(o) == str(current) else '')}{o}", callback_data=f'nsfwcfg:{request_id}:{field}:{o}') for o in options]
    rows = [[InlineKeyboardButton(text='✏️ Промпт', callback_data=f'nsfwinput:{request_id}:prompt'), InlineKeyboardButton(text='🚫 Негативный', callback_data=f'nsfwinput:{request_id}:neg')], [InlineKeyboardButton(text='— Шаги —', callback_data='noop')], row('steps', _NSFW_STEPS, cur_steps), [InlineKeyboardButton(text='— CFG Scale —', callback_data='noop')], row('cfg', _NSFW_CFG, cur_cfgv), [InlineKeyboardButton(text='— Размер —', callback_data='noop')], [InlineKeyboardButton(text=f"{('✅' if s == cur_size else '')}{s}", callback_data=f'nsfwcfg:{request_id}:size:{s}') for s in _NSFW_SIZES[:3]], [InlineKeyboardButton(text=f"{('✅' if s == cur_size else '')}{s}", callback_data=f'nsfwcfg:{request_id}:size:{s}') for s in _NSFW_SIZES[3:]]]
    if is_wai:
        cur_batch = cfg.get('batch', 1)
        rows += [[InlineKeyboardButton(text='— Количество изображений —', callback_data='noop')], [InlineKeyboardButton(text=f"{('✅' if i == cur_batch else '')}{i}", callback_data=f'nsfwcfg:{request_id}:batch:{i}') for i in range(1, 5)], [InlineKeyboardButton(text='— Планировщик (Sampler) —', callback_data='noop')], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[:3]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[3:6]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[6:9]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[9:12]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[12:15]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[15:18]], [InlineKeyboardButton(text=f"{('✅' if s == cur_sched else '')}{s}", callback_data=f'nsfwcfg:{request_id}:scheduler:{s}') for s in _NSFW_SCHEDULERS[18:]], [InlineKeyboardButton(text='— CLIP Skip —', callback_data='noop')], row('clip_skip', _NSFW_CLIP_SKIP, cur_clip), [InlineKeyboardButton(text='— PAG Scale (улучшение качества) —', callback_data='noop')], row('pag_scale', _NSFW_PAG, cur_pag), [InlineKeyboardButton(text='— Guidance Rescale —', callback_data='noop')], row('rescale', _NSFW_RESCALE, cur_resc), [InlineKeyboardButton(text=f"{('✅' if cur_pre else '❌')} Препромпт качества", callback_data=f"nsfwcfg:{request_id}:prepend:{('0' if cur_pre else '1')}"), InlineKeyboardButton(text=f'🎲 Seed: {cur_seed}', callback_data=f'nsfwinput:{request_id}:seed')]]
    rows.append([InlineKeyboardButton(text='🚀 Генерировать', callback_data=f'nsfwgen:{request_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

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
        err_msg = await safe_send(bot.send_message, chat_id=request_data['chat_id'], text=f'❌ Ошибка:\n{error_msg}\n\n⏳ Ща спрошу у мозгов, че не так...', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)
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
                await safe_send(bot.edit_message_text, chat_id=request_data['chat_id'], message_id=err_msg.message_id, text=f'❌ Ошибка:\n{error_msg}\n\n🧠 Пояснение:\n{explanation}')
            except Exception:
                pass
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

def _prompt_ai_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Использовать этот промт', callback_data=f'puse:{request_id}')], [InlineKeyboardButton(text='🔄 Другой вариант', callback_data=f'pother:{request_id}'), InlineKeyboardButton(text='📝 Мой промт', callback_data=f'pbase:{request_id}')]])

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
        await message.reply('Напиши промпт после команды, например:\n/video закат над морем\n\nИли прикрепи фото с подписью /video анимируй это — Veo оживит картинку.')
        return
    image_bytes = None
    if message.photo:
        photo = message.photo[-1]
        file_info = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        image_bytes = downloaded.read()
    request_id = uuid.uuid4().hex[:10]
    pending_video_requests[request_id] = {'user_id': message.from_user.id, 'chat_id': message.chat.id, 'source_message_id': message.message_id, 'message_thread_id': message.message_thread_id if message.chat.is_forum else None, 'prompt': prompt, 'image_bytes': image_bytes}
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
        asyncio.create_task(add_user_stat(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'video'))
        asyncio.create_task(log_prompt(request_data.get('user_id', 0), request_data.get('username', ''), request_data.get('first_name', 'Аноним'), 'video', request_data.get('prompt', '')))
        return
    await callback.bot.send_message(chat_id=request_data['chat_id'], text='❌ Не удалось получить видео.', reply_to_message_id=request_data['source_message_id'], **reply_kwargs)

@media_router.message(Command('tts'))
async def cmd_tts(message: types.Message):
    _track_user(message)
    is_member = await check_membership(message.bot, message.from_user.id, message.chat.id)
    if not is_member and message.chat.type != 'private':
        await message.reply('Доступ запрещен.')
        return
    current_time = time.time()
    last_time = user_video_cooldowns.get(message.from_user.id, 0)
    if current_time - last_time < 30:
        await message.reply(f'Подожди еще {int(30 - (current_time - last_time))} сек.')
        return
    user_video_cooldowns[message.from_user.id] = current_time
    prompt = (message.text or '').replace('/tts', '').strip()
    if not prompt:
        await message.reply('Напиши текст после команды, например:\n/tts Привет, ублюдок!')
        return
    request_id = uuid.uuid4().hex[:10]
    thread_id = message.message_thread_id if message.chat.is_forum else None
    pending_tts_requests[request_id] = {'user_id': message.from_user.id, 'chat_id': message.chat.id, 'source_message_id': message.message_id, 'message_thread_id': thread_id, 'prompt': prompt, 'username': message.from_user.username or '', 'first_name': message.from_user.first_name or 'Аноним'}
    if not TTS_MODELS:
        await message.reply('Нет доступных TTS моделей.')
        return
    rows = [[InlineKeyboardButton(text=label, callback_data=f'ttssel:{request_id}:{mid}')] for (mid, (label, _)) in TTS_MODELS.items()]
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    reply_kwargs = {}
    if message.chat.is_forum and thread_id:
        reply_kwargs['message_thread_id'] = thread_id
    await message.reply('Выберите модель TTS:', reply_markup=keyboard, **reply_kwargs)

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
    d = pending_tts_configs.pop(request_id, None)
    if not d:
        await callback.answer('Запрос устарел.', show_alert=True)
        return
    if callback.from_user.id != d['user_id']:
        await callback.answer('Только автор.', show_alert=True)
        return
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

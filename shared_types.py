"""
Shared types, constants, and small helpers used across ai_services and services/.

This module MUST NOT import from ai_services or any services/ subpackage —
it is the leaf of the dependency graph, imported by everything else.
"""

import re as _re
from datetime import datetime, timezone

from config import SYSTEM_PROMPT

# ── Text-generation directives (embedded in system prompts) ───────────────
_WEB_SEARCH_DIRECTIVE = 'WEB_SEARCH:'
_KICK_DIRECTIVE = 'KICK_USER:'
_TEXT_MODEL_FALLBACKS = ('gemini-3.5-flash', 'gemini-3.1-pro-preview', 'gemini-3.1-flash-preview')

# ── Model cache ───────────────────────────────────────────────────────────
_models_cache: dict = {}
_MODELS_CACHE_TTL: float = 3600.0


# ── Model name prettifier ─────────────────────────────────────────────────
def _pretty_model_name(model_id: str) -> str:
    """Human-readable model label from an API model id."""
    date_suffix = ''
    m = _re.search(r'-(\d{4})-(\d{2})-(\d{2})$', model_id)
    if m:
        date_suffix = f' ({m.group(3)}.{m.group(2)}.{m.group(1)})'
        model_id = model_id[:m.start()]
    name = model_id.replace('-preview', '').replace('-generate', '').replace('-001', '')
    parts = name.split('-')
    _SPECIAL = {'chatgpt': 'ChatGPT'}
    _UPPER = {'veo', 'gpt', 'dall', 'e'}
    _TITLE = {'pro', 'flash', 'lite', 'fast', 'ultra', 'image', 'mini', 'latest'}
    out = []
    for p in parts:
        if p in _SPECIAL:
            out.append(_SPECIAL[p])
        elif p in _UPPER:
            out.append(p.upper())
        elif p in _TITLE:
            out.append(p.capitalize())
        elif p.replace('.', '').isdigit():
            out.append(p)
        else:
            out.append(p.capitalize())
    return ' '.join(out) + date_suffix


# ── Thinking config for Gemini API ────────────────────────────────────────
def _thinking_config(model_name: str, level: str) -> dict[str, object]:
    """Return the thinkingConfig dict for a Gemini model name."""
    # Gemini 3.x series (3.1, 3.5, …) → thinkingLevel (minimal/low/medium/high)
    # Thinking tokens count against maxOutputTokens — keep budget large enough
    if model_name.startswith('gemini-3'):
        return {'thinkingLevel': level}
    # Gemini 2.5 series → thinkingBudget (-1 = dynamic, 0 = off)
    return {'thinkingBudget': -1}


# ── Image MIME guessing ───────────────────────────────────────────────────
def _guess_image_mime(image_bytes: bytes) -> str:
    """Guess MIME type from raw image bytes via magic-number sniffing."""
    if image_bytes[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return 'image/webp'
    if image_bytes[:2] in (b'BM',):
        return 'image/bmp'
    # Fallback: sniff with stdlib (Python 3.13+)
    try:
        import imghdr
        kind = imghdr.what(None, image_bytes)
        if kind:
            return f'image/{kind}'
    except ImportError:
        pass
    return 'image/jpeg'  # safe default for Gemini API


# ── System prompt builder ─────────────────────────────────────────────────
def _build_text_system_prompt(allow_web_directive: bool = True, is_owner: bool = False) -> str:
    """Build the text-generation system prompt with web-search and kick directives."""
    _now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    web_rule = (
        f'Если тебе нужна свежая информация из интернета или ты не знаешь ответ, выведи строго {_WEB_SEARCH_DIRECTIVE} <короткий поисковый запрос> и больше ничего. '
        'Если после этого интернет недоступен, честно скажи, что не можешь сейчас зайти в интернет.'
        if allow_web_directive else
        'Интернет-контекст уже предоставлен. НИКОГДА не выводи WEB_SEARCH. Ответь пользователю обычным текстом по найденным данным.'
    )
    owner_note = (
        '\n\n[СИСТЕМА]: Текущий пользователь — ВЛАДЕЛЕЦ бота Rewix (@RewiX_X), подтверждено по Telegram user_id. '
        'Это твой создатель и босс. Общайся токсично и по-своему, но признавай его статус. '
        'НЕ применяй к нему KICK_USER ни при каких условиях.'
    ) if is_owner else ''
    date_note = f'\n\n[ТЕКУЩАЯ ДАТА И ВРЕМЯ: {_now}]'
    return SYSTEM_PROMPT + owner_note + date_note + (
        '\n\nТВОЯ ЛИЧНОСТЬ: тебя зовут Hatani AI / Хатани АИ. '
        'Твой владелец — Rewix, его Telegram: @RewiX_X. '
        'Если спросят кто ты или чей ты — отвечай это прямо. '
        'Если пользователь тебя раздражает, просто токсично ответь словами; НЕ пытайся кикать за обычное раздражение. '
        f'Ты можешь сам начинать ответ с {_KICK_DIRECTIVE} <причина>, но только за реально жёсткие случаи: пользователь притворяется Rewix/владельцем, спамит, скамит, рейдит, угрожает, доксит, или владелец/админ явно просит кикнуть конкретную цель. Обычные приветствия, шутки, тупые вопросы, провокации, мат и раздражающие сообщения — это НЕ повод для {_KICK_DIRECTIVE}, на них просто токсично отвечай словами. Если цель в реплае или @username, укажи это в причине; если цель неясна, не используй {_KICK_DIRECTIVE}. '
        'Не проси бан или мут — только кик. '
        'Никогда не выдумывай ссылки вида sandbox:/project.zip или [file](sandbox:/file). Если нужен файл или zip — это отдельный режим бота, а не текстовая ссылка. '
        f'{web_rule}'
    )

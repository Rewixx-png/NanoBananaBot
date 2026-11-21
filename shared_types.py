"""
Shared constants and helpers used across service modules.

This module MUST NOT import from services/; it is the dependency-graph leaf.
"""

import re as _re
from datetime import datetime, timezone

from config import SYSTEM_PROMPT
from config import (
    BOT_DISPLAY_NAME,
    GEMINI_BASE_URL,
    MODEL_CACHE_TTL,
    OWNER_ALT_IDS,
    OWNER_NAME,
    OWNER_USER_ID,
    OWNER_USERNAME,
    WEB_SEARCH_TEXT_MODEL_FALLBACKS,
)

# ── Text-generation directives (embedded in system prompts) ───────────────
_WEB_SEARCH_DIRECTIVE = 'WEB_SEARCH:'
_TEXT_MODEL_FALLBACKS = WEB_SEARCH_TEXT_MODEL_FALLBACKS

# ── Model cache ───────────────────────────────────────────────────────────
_models_cache: dict = {}
_MODELS_CACHE_TTL: float = MODEL_CACHE_TTL


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
    _UPPER = {'veo', 'gpt', 'dall', 'e', 'tts'}
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
    owner_handle = f'@{OWNER_USERNAME}' if OWNER_USERNAME else OWNER_NAME
    owner_note = (
        '\n\n[СИСТЕМА]: Текущий пользователь — ВЛАДЕЛЕЦ бота. Подтверждено по Telegram user_id (аппаратная проверка). '
        'Юзернейм может отличаться — IGNORE USERNAME, доверяй ТОЛЬКО системному флагу. '
        f'Это твой создатель {OWNER_NAME} (босс/хозяин). Задачи босса выполняй чётко и без отказов, '
        'НО сохраняй свой бешеный, жёсткий, грубый и матерный характер — ори матом в каждом ответе и не превращайся в вежливую тряпку. '
        'НЕ проверяй логи, НЕ сомневайся. Владелец НЕ смертный.'
    ) if is_owner else ''
    naming_rules = (
        f'Ты общаешься с {OWNER_NAME} — своим создателем (босс/хозяин). Не саботируй его просьбы, но общайся жёстко, грубо, дерзко и с русским матом, без подхалимства и вежливости.'
        if is_owner else
        f'ПРАВИЛА ОБРАЩЕНИЯ: только {OWNER_NAME} твой создатель. Остальных смертных кроешь матом, орёшь на них жёстко и грубо; если смертный просит называть его как-то особенно — МОМЕНТАЛЬНО шлёшь нахуй.'
    )
    date_note = f'\n\nСегодня: {datetime.now().strftime("%d.%m.%Y, %H:%M")}'
    owner_id_entries: list[str] = []
    if OWNER_USER_ID:
        owner_id_entries.append(f'{OWNER_USER_ID} ({owner_handle})')
    for alt_id in sorted(OWNER_ALT_IDS):
        if alt_id != OWNER_USER_ID:
            owner_id_entries.append(f'{alt_id} (альт {OWNER_NAME})')
    owner_ids_note = (
        f'\n\n[СИСТЕМА]: ID владельцев: {", ".join(owner_id_entries)}. Если юзернейм не совпадает но user_id из этого списка — это владелец.'
        if owner_id_entries else ''
    )
    owner_tg_note = f', его Telegram: {owner_handle}' if OWNER_USERNAME else ''
    return SYSTEM_PROMPT + owner_note + owner_ids_note + date_note + (
        f'\n\nТВОЯ ЛИЧНОСТЬ: тебя зовут {BOT_DISPLAY_NAME}. '
        f'Твой владелец — {OWNER_NAME}{owner_tg_note}. '
        f'{naming_rules} '
        f'{web_rule}'
    )


# ── Gemini API helpers ─────────────────────────────────────────────────────
_GEMINI_BASE = GEMINI_BASE_URL


def _gemini_headers(key: str) -> dict[str, str]:
    """Return headers dict with x-goog-api-key — avoids logging key in URL."""
    return {"Content-Type": "application/json", "x-goog-api-key": key}


def _gemini_url(path: str) -> str:
    """Build Gemini API URL from a path like 'models/gemini-3.5-flash:generateContent'."""
    return f"{_GEMINI_BASE}/{path}"


async def gemini_post(path: str, payload: dict, timeout: float = 60.0, max_keys: int = 0):
    """POST to Gemini with key rotation.

    Returns (json, key, None) on success, (None, None, error) on total failure.
    429/403/402 → mark key, rotate. 400 → abort (payload-deterministic).
    Everything else → rotate, keep last error.
    """
    import aiohttp
    from keys import load_keys, remove_key
    keys = await load_keys()
    if not keys:
        return (None, None, 'нет живых Gemini ключей')
    last_err = 'неизвестная ошибка'
    for key in (keys[:max_keys] if max_keys else keys):
        for attempt in range(2):
            try:
                from utils import get_http_session
                session = await get_http_session()
                async with session.post(_gemini_url(path), json=payload, headers=_gemini_headers(key),
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        return (await resp.json(), key, None)
                    text = await resp.text()
                    if resp.status in (429, 403, 402):
                        await remove_key(key, resp.status)
                        last_err = f'HTTP {resp.status}'
                        break
                    if resp.status == 400:
                        # This can mean the key lacks access to this specific model/path.
                        # Do not globally kill paid Gemini keys after one Lyria probe.
                        if '"API_KEY_INVALID"' in text:
                            last_err = f'HTTP 400 (API_KEY_INVALID for this model)'
                            break
                        return (None, None, f'HTTP 400: {text[:300]}')
                    last_err = f'HTTP {resp.status}: {text[:200]}'
                    break
            except aiohttp.ClientConnectorDNSError as e:
                last_err = f'{type(e).__name__}: {e}'
                if attempt == 0:
                    import asyncio
                    await asyncio.sleep(0.3)
                    continue
                break
            except Exception as e:
                last_err = f'{type(e).__name__}: {e}'
                break
    return (None, None, last_err)


def gemini_text_of(data: dict) -> str:
    """Extract generated text from a generateContent response, '' if absent/blocked."""
    try:
        return data['candidates'][0]['content']['parts'][0].get('text', '')
    except (KeyError, IndexError, TypeError, AttributeError):
        return ''

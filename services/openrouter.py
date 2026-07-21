import asyncio
import base64
import json
import logging
import time
import aiohttp
from typing import Tuple, Optional, List, Dict, Any

from keys import load_openrouter_keys, remove_key
from services.security_utils import is_safe_url
from config import (
    OPENROUTER_APP_TITLE,
    OPENROUTER_BASE_URL,
    OPENROUTER_POLICY_COOLDOWN_SECONDS,
    OPENROUTER_TEXT_MODEL,
    OPENROUTER_VISION_MODEL,
    PHOTO_ANALYSIS_MAX_TOKENS,
    PHOTO_ANALYSIS_TIMEOUT,
)
from shared_types import _guess_image_mime



_TEXT_POLICY_DEAD: dict[str, float] = {}
_TEXT_POLICY_COOLDOWN_SECONDS = OPENROUTER_POLICY_COOLDOWN_SECONDS


async def openrouter_chat(
    messages: list[dict],
    system_prompt: str = "",
    model: str = OPENROUTER_TEXT_MODEL,
    max_tokens: int = 800,
    tools: Optional[list[dict]] = None,
    timeout: int = 90,
) -> dict:
    """Return one raw OpenAI-compatible completion from an exact OpenRouter model."""
    loaded_keys = await load_openrouter_keys()
    now = time.monotonic()
    for key, expiry in list(_TEXT_POLICY_DEAD.items()):
        if expiry <= now:
            del _TEXT_POLICY_DEAD[key]
    api_keys = [key for key in loaded_keys if key not in _TEXT_POLICY_DEAD]
    if not api_keys:
        raise RuntimeError("OpenRouter: нет доступных API-ключей; policy-blocked ключи временно в cooldown")

    request_messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + messages
    payload: dict[str, Any] = {
        "model": model,
        "messages": request_messages,
        "max_tokens": max_tokens,
        "reasoning": {"effort": "none"},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    last_error = "provider returned no response"
    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession() as session:
        for api_key in api_keys:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-OpenRouter-Title": OPENROUTER_APP_TITLE,
            }
            try:
                async with session.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=request_timeout,
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("choices"):
                            return data
                        last_error = "успешный ответ не содержит choices"
                        continue

                    body = await response.text()
                    last_error = f"HTTP {response.status}: {body[:500]}"
                    if response.status in (401, 402, 403, 429):
                        remove_key(api_key, response.status)
                    if response.status == 404:
                        _TEXT_POLICY_DEAD[api_key] = time.monotonic() + _TEXT_POLICY_COOLDOWN_SECONDS
                        logging.warning("OpenRouter key is in a %ss cooldown after policy/guardrail 404", _TEXT_POLICY_COOLDOWN_SECONDS)
                    if response.status == 400:
                        break
            except asyncio.TimeoutError:
                last_error = f"таймаут после {timeout} секунд"
            except Exception as error:
                last_error = f"{type(error).__name__}: {error}"

    raise RuntimeError(f"OpenRouter {model}: {last_error}")


def _response_text(data: dict, model: str) -> str:
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        raise RuntimeError(f"OpenRouter {model}: успешный ответ не содержит текста")
    return content


async def generate_text_with_openrouter(
    prompt: str,
    system_prompt: str = "",
    model: str = OPENROUTER_TEXT_MODEL,
    max_tokens: int = 800,
    timeout: int = 90,
) -> str:
    """Generate text through one exact OpenRouter model; never substitute another model."""
    data = await openrouter_chat(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=system_prompt,
        model=model,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return _response_text(data, model)


async def analyze_image_with_openrouter(image_bytes: bytes, prompt: str) -> str:
    """Analyze one local image through the configured OpenRouter vision model."""
    data_url = f"data:{_guess_image_mime(image_bytes)};base64,{base64.b64encode(image_bytes).decode()}"
    data = await openrouter_chat(
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt or "Что на этом фото? Опиши подробно."},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        model=OPENROUTER_VISION_MODEL,
        max_tokens=PHOTO_ANALYSIS_MAX_TOKENS,
        timeout=PHOTO_ANALYSIS_TIMEOUT,
    )
    return _response_text(data, OPENROUTER_VISION_MODEL)



async def generate_image_with_openrouter(
    prompt: str,
    model: str = 'google/gemini-3.1-flash-image-preview',
    images_bytes: Optional[List[bytes]] = None,
    state_data: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Generate an image via OpenRouter API (multi-provider proxy with unified chat-completions interface)."""
    api_keys = await load_openrouter_keys()
    if not api_keys:
        return (None, 'Нет ключей OpenRouter. Добавьте sk-or-... ключи в r.txt.')
    prompt_text = prompt if prompt else 'A highly detailed beautiful picture'
    url = f'{OPENROUTER_BASE_URL}/chat/completions'
    modalities = ['image'] if 'flux' in model or 'seedream' in model or 'riverflow' in model else ['image', 'text']
    msg_content = [{'type': 'text', 'text': prompt_text}]
    if images_bytes:
        for img in images_bytes[:4]:
            b64 = base64.b64encode(img).decode()
            msg_content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}})
    payload = {'model': model, 'modalities': modalities, 'messages': [{'role': 'user', 'content': msg_content}]}
    request_timeout = aiohttp.ClientTimeout(total=300)
    last_error = None
    for idx, api_key in enumerate(api_keys):
        if state_data:
            state_data['status'] = f'Пробую ключ {idx+1}/{len(api_keys)} (OpenRouter)'
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=headers, timeout=request_timeout) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        raw_text = raw.decode('utf-8', errors='replace')
                        try:
                            import re as _re
                            b64_match = _re.search('data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', raw_text)
                            if b64_match:
                                return (base64.b64decode(b64_match.group(1)), None)
                        except Exception:
                            pass
                        try:
                            d = json.loads(raw_text)
                            msg = d.get('choices', [{}])[0].get('message', {})
                            for src in [msg.get('images', []), msg.get('content', []) or []]:
                                for part in src:
                                    if isinstance(part, dict) and part.get('type') == 'image_url':
                                        img_url = part.get('image_url', {}).get('url', '')
                                        if img_url.startswith('data:'):
                                            return (base64.b64decode(img_url.split(',', 1)[1]), None)
                                        if img_url.startswith('http'):
                                            if not is_safe_url(img_url):
                                                logging.warning(f'OpenRouter returned unsafe image URL, blocked: {img_url[:120]}')
                                                continue
                                            async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=60)) as img_resp:
                                                if img_resp.status == 200:
                                                    return (await img_resp.read(), None)
                        except Exception:
                            pass
                        return (None, 'OpenRouter не вернул изображение в ответе.')
                    err_text = await resp.text()
                    last_error = f'Ошибка OpenRouter ({resp.status}): {err_text[:200]}'
                    if resp.status in [401, 403]:
                        logging.warning(f'OpenRouter {resp.status} на ключе {api_key[:12]}..., пробую следующий.')
                        remove_key(api_key, resp.status)
                        continue
            except asyncio.TimeoutError:
                last_error = 'Таймаут OpenRouter'
                continue
            except Exception as e:
                last_error = str(e)
                continue
    return (None, last_error)

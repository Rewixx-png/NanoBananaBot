"""Lyria music generation — text-to-music via Gemini Interactions API."""

import asyncio
import base64
import logging
import aiohttp
from typing import Tuple, Optional

from keys import load_keys, remove_key

logger = logging.getLogger(__name__)

# Model IDs (via Interactions API)
LYRIA_CLIP = "lyria-3-clip-preview"      # 30s clips, MP3
LYRIA_PRO  = "lyria-3-pro-preview"        # Full songs, MP3/WAV
LYRIA_RT   = "lyria-realtime-exp"         # Real-time streaming

MUSIC_MODELS = {
    "lyria-clip":  {"id": LYRIA_CLIP, "label": "🎵 Lyria 3 Clip",   "desc": "30-секундные клипы, MP3"},
    "lyria-pro":   {"id": LYRIA_PRO,  "label": "🎼 Lyria 3 Pro",    "desc": "Полноценные песни до 2 мин, MP3/WAV"},
    "lyria-rt":    {"id": LYRIA_RT,   "label": "🎹 Lyria RealTime", "desc": "Стриминг в реальном времени, hi-fi"},
}

MUSIC_MODEL_LIST = list(MUSIC_MODELS.keys())

async def fetch_music_models() -> list:
    """Return available Lyria models for inline keyboard selection."""
    return [
        {"id": k, "label": v["label"], "desc": v["desc"]}
        for k, v in MUSIC_MODELS.items()
    ]


async def generate_music(
    prompt: str,
    model_key: str = "lyria-clip",
    output_format: str = "mp3",
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Generate music via Lyria Interactions API. Falls back through all models."""
    if model_key not in MUSIC_MODELS:
        return None, None, f"Неизвестная модель: {model_key}"

    keys = await load_keys()
    if not keys:
        return None, None, "Нет доступных Gemini ключей."

    # Fallback chain: selected model → other models
    model_chain = [model_key] + [k for k in MUSIC_MODEL_LIST if k != model_key]
    errors = []

    for mk in model_chain:
        info = MUSIC_MODELS[mk]
        model_id = info["id"]

        payload = {
            "model": model_id,
            "input": prompt,
        }
        if output_format == "wav" and mk == "lyria-pro":
            payload["response_format"] = {"type": "audio"}

        for key in keys[:5]:
            try:
                async with aiohttp.ClientSession() as s:
                    url = f"https://generativelanguage.googleapis.com/v1beta/interactions?key={key}"
                    async with s.post(
                        url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            audio_b64 = None
                            lyrics_parts = []

                            steps = data.get("steps", [])
                            for step in steps:
                                if step.get("type") == "model_output":
                                    for block in step.get("content", []):
                                        if block.get("type") == "audio":
                                            audio_b64 = block.get("data", "")
                                        elif block.get("type") == "text":
                                            lyrics_parts.append(block.get("text", ""))

                            if not audio_b64:
                                out_audio = data.get("output_audio")
                                if out_audio and isinstance(out_audio, dict):
                                    audio_b64 = out_audio.get("data", "")

                            audio_bytes = base64.b64decode(audio_b64) if audio_b64 else None
                            lyrics = "\n".join(lyrics_parts) or data.get("output_text") or None

                            if audio_bytes:
                                logger.info(f"music: generated {len(audio_bytes)//1024}KB via {mk}")
                                return audio_bytes, lyrics, None

                            errors.append(f"{mk}: empty audio")
                            logger.warning(f"music: {mk}: empty audio in response")

                        elif resp.status in (429, 403):
                            errors.append(f"{mk}: HTTP {resp.status}")
                            continue
                        elif resp.status == 400:
                            body = await resp.text()
                            errors.append(f"{mk}: HTTP 400")
                            break
                        else:
                            body = await resp.text()
                            errors.append(f"{mk}: HTTP {resp.status}")
                            logger.warning(f"music: {mk}: HTTP {resp.status}: {body[:100]}")
            except asyncio.TimeoutError:
                errors.append(f"{mk}: timeout")
            except Exception as e:
                errors.append(f"{mk}: {type(e).__name__}")
    # All models exhausted — give honest error
    first = errors[0] if errors else "неизвестная ошибка"
    rest = f"; +{len(errors)-1}" if len(errors) > 1 else ""
    return None, None, f"Lyria API недоступна: {first}{rest}. Нужны ключи с квотой на Interactions API (отдельно от generateContent)."

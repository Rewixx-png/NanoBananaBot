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
    """
    Generate music via Lyria Interactions API.

    Returns: (audio_bytes, lyrics_text, error_string)
    - audio_bytes: raw MP3/WAV data or None on failure
    - lyrics_text: generated lyrics or None
    - error_string: error description or None on success
    """
    model_info = MUSIC_MODELS.get(model_key)
    if not model_info:
        return None, None, f"Неизвестная модель: {model_key}"

    model_id = model_info["id"]
    keys = await load_keys()
    if not keys:
        return None, None, "Нет доступных Gemini ключей."

    payload = {
        "model": model_id,
        "input": prompt,
    }

    # WAV output for Pro model if requested
    if output_format == "wav" and model_key == "lyria-pro":
        payload["response_format"] = {"type": "audio"}

    last_error = None
    for key in keys[:5]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://generativelanguage.googleapis.com/v1beta/interactions",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": key,
                    },
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()

                        # Extract audio
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

                        # Also try convenience properties
                        if not audio_b64:
                            out_audio = data.get("output_audio")
                            if out_audio and isinstance(out_audio, dict):
                                audio_b64 = out_audio.get("data", "")

                        audio_bytes = base64.b64decode(audio_b64) if audio_b64 else None
                        lyrics = "\n".join(lyrics_parts) or data.get("output_text") or None

                        if audio_bytes:
                            logger.info(
                                f"music: generated {len(audio_bytes)//1024}KB"
                                f"{' + lyrics' if lyrics else ''}"
                            )
                            return audio_bytes, lyrics, None

                        last_error = f"{model_id}: empty audio in response"
                        logger.warning(f"music: {last_error}")

                    elif resp.status in (429, 403):
                        last_error = f"{model_id}: HTTP {resp.status} (rate limited)"
                        remove_key(key, resp.status)
                        continue
                    elif resp.status == 400:
                        body = await resp.text()
                        last_error = f"{model_id}: HTTP 400 — {body[:200]}"
                        # Don't retry on 400 (bad request)
                        break
                    else:
                        body = await resp.text()
                        last_error = f"{model_id}: HTTP {resp.status} — {body[:150]}"
                        logger.warning(f"music: {last_error}")
        except asyncio.TimeoutError:
            last_error = f"{model_id}: timeout (120s)"
        except Exception as e:
            last_error = f"{model_id}: {type(e).__name__}: {e}"
            logger.warning(f"music: {last_error}")

    return None, None, last_error or f"{model_id}: все ключи исчерпаны"

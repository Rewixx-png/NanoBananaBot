"""ElevenLabs API service — TTS, voice cloning, music, SFX, isolator, STT, voice design.

Docs: https://elevenlabs.io/docs/overview
- Voice cloning: remove_background_noise=true (built-in, skip separate isolator)
- TTS models: eleven_flash_v2_5 (default, 75ms, 32 langs), eleven_multilingual_v2, eleven_v3
- STT: scribe_v2 model
- Uses a PERSISTENT aiohttp.ClientSession — avoids "Connection closed" from per-request session teardown
"""
import asyncio
import logging
import time
from typing import Optional

import aiohttp

from config import (
    ELEVENLABS_BASE_URL,
    ELEVENLABS_CLONE_TIMEOUT,
    ELEVENLABS_COOLDOWN_SECONDS,
    ELEVENLABS_ISOLATOR_TIMEOUT,
    ELEVENLABS_MUSIC_DURATION_MS,
    ELEVENLABS_MUSIC_MODEL,
    ELEVENLABS_MUSIC_TIMEOUT,
    ELEVENLABS_SFX_MODEL,
    ELEVENLABS_SFX_TIMEOUT,
    ELEVENLABS_STT_MODEL,
    ELEVENLABS_STT_TIMEOUT,
    ELEVENLABS_TTS_MODEL,
    ELEVENLABS_TTS_TIMEOUT,
    ELEVENLABS_VOICE_CHANGER_MODEL,
    ELEVENLABS_VOICE_CHANGER_TIMEOUT,
    ELEVENLABS_VOICE_DESIGN_TIMEOUT,
)

logger = logging.getLogger(__name__)

TIER_ORDER = {'enterprise': 0, 'business': 1, 'scale': 2, 'pro': 3, 'creator': 4, 'starter': 5, 'free': 6}

# Persistent session — lives for the bot's lifetime, avoids Connection closed
_session: Optional[aiohttp.ClientSession] = None

# Per-key cooldown tracking
_dead_until: dict[str, float] = {}


def _parse_tier(info: str) -> str:
    """Extract tier from ElevenLabs info string.
    Handles both: 'scale_2024_08_10', 'pro', and 'ElevenLabs LIVE (tier: free, chars: 6/10000)'."""
    if not info:
        return "unknown"
    # New format: "ElevenLabs LIVE (tier: free, chars: 6/10000)"
    import re
    m = re.search(r'tier:\s*(\S+)', info)
    if m:
        tier = m.group(1).rstrip(',)')
    else:
        # Old format: just the tier name, possibly with suffix like scale_2024_08_10
        tier = info.split("_")[0].lower() if "_" in info else info.lower()
    # Normalize: scale_2024_08_10 → scale, growing_business → business
    tier = tier.split("_")[0]
    if tier == "growing":
        tier = "business"
    return tier


async def load_elevenlabs_keys() -> list[dict]:
    """Load ElevenLabs API keys from keyhunter.db, ordered by tier priority (best first)."""
    import aiosqlite
    from keys.manager import REWTEST_DB
    now = time.time()
    try:
        async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
            async with db.execute(
                "SELECT key, info FROM keys WHERE service='ElevenLabs' AND is_live=1"
            ) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        logger.warning(f"load_elevenlabs_keys: {e}")
        return []
    keys = []
    for key, info in rows:
        if key in _dead_until and now < _dead_until[key]:
            continue
        tier = _parse_tier(info or "")
        # Extract chars remaining: "chars: 6/10000" → 10000-6=9994; "chars: 2000000/2000000" → 0
        chars = 0
        m = __import__('re').search(r'chars:\s*(\d+)/(\d+)', info or "")
        if m:
            used, total = int(m.group(1)), int(m.group(2))
            chars = total - used
        if chars <= 0:
            continue  # skip depleted keys
        keys.append({"key": key, "tier": tier, "info": info, "chars": chars})
    keys.sort(key=lambda k: TIER_ORDER.get(k["tier"], 99))
    logger.info(f"ElevenLabs: {len(keys)} usable keys (top tier: {keys[0]['tier'] if keys else 'none'})")
    return keys


def mark_key_dead(key: str, status_code: int = 429):
    """Put key into cooldown after rate-limit/auth errors."""
    _dead_until[key] = time.time() + ELEVENLABS_COOLDOWN_SECONDS
    logger.info(f"ElevenLabs key cooldown ({status_code}): {key[:20]}...")


async def _get_session() -> aiohttp.ClientSession:
    """Lazy-init shared persistent session. Never closed until bot shutdown."""
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, force_close=False)
        timeout = aiohttp.ClientTimeout(total=120, connect=15, sock_read=30)
        _session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _session


async def close_elevenlabs():
    """Gracefully close the shared session (call on bot shutdown)."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


async def _request(method: str, endpoint: str, key: str, **kwargs) -> Optional[aiohttp.ClientResponse]:
    """Make an authenticated request to ElevenLabs API.
    Uses shared persistent session — TCP stays alive across calls."""
    url = f"{ELEVENLABS_BASE_URL}/{endpoint}"
    headers = {"xi-api-key": key, **kwargs.pop("headers", {})}
    timeout = aiohttp.ClientTimeout(total=kwargs.pop("timeout", 60), sock_read=30)
    session = await _get_session()
    return await session.request(method, url, headers=headers, timeout=timeout, **kwargs)


async def _try_with_keys(endpoint: str, method: str = "POST", **kwargs) -> tuple[Optional[aiohttp.ClientResponse], Optional[dict], Optional[str]]:
    """Try request with key rotation. Returns (response, key_info, last_error)."""
    keys = await load_elevenlabs_keys()
    if not keys:
        return None, None, "нет ключей"
    last_error = None
    for ki in keys:
        try:
            resp = await _request(method, endpoint, ki['key'], **kwargs)
            if resp.status == 200:
                return resp, ki, None
            if resp.status in (429, 401, 402):  # real auth/rate-limit issues
                mark_key_dead(ki['key'], resp.status)
                last_error = f"HTTP {resp.status} on {ki['tier']}"
                continue
            if resp.status in (403, 404):  # voice on different account or needs verification
                body = await _safe_resp_text(resp)
                last_error = f"HTTP {resp.status}: {body[:150]}"
                continue
            if resp.status >= 500:
                last_error = f"HTTP {resp.status} (server error)"
                continue
            # Other 4xx client errors — don't retry
            body = await _safe_resp_text(resp)
            return resp, ki, f"HTTP {resp.status}: {body[:200]}"
        except Exception as e:
            logger.warning(f"ElevenLabs request failed for key {ki['key'][:20]}...: {e}")
            last_error = str(e)[:200]
            continue
    return None, None, last_error or "все ключи пропущены"

async def _safe_resp_text(resp) -> str:
    """Safely read response body — handles Connection closed."""
    try:
        return await asyncio.wait_for(resp.text(), timeout=30)
    except Exception:
        return "(connection closed)"


async def _safe_resp_json(resp) -> Optional[dict]:
    """Safely read response JSON — handles Connection closed and timeouts."""
    try:
        return await asyncio.wait_for(resp.json(), timeout=30)
    except Exception as e:
        logger.warning(f"resp.json failed: {type(e).__name__}: {e}")
        return None


async def _safe_resp_read(resp, timeout: int = 120) -> Optional[bytes]:
    """Safely read response body bytes — handles Connection closed and timeouts."""
    try:
        return await asyncio.wait_for(resp.read(), timeout=timeout)
    except Exception as e:
        logger.warning(f"resp.read failed: {type(e).__name__}: {e}")
        return None

async def _require_audio_response(service: str, response, error: Optional[str], timeout: int) -> bytes:
    if response and response.status == 200:
        audio = await _safe_resp_read(response, timeout=timeout)
        if audio:
            return audio
        error = "empty or unreadable audio response"
    elif response:
        body = await _safe_resp_text(response)
        error = f"HTTP {response.status}: {body[:300]}"
    message = error or "provider returned no response"
    logger.error(f"{service} failed: {message}")
    raise RuntimeError(f"{service}: {message}")


# ── TTS ────────────────────────────────────────────────────────────────────

async def elevenlabs_tts(text: str, voice_id: str, model: str = ELEVENLABS_TTS_MODEL,
                         stability: float = 0.5, similarity_boost: float = 0.75,
                         style: float = 0.0, speed: float = 1.0) -> Optional[bytes]:
    """Generate speech from text. Returns mp3 bytes or raises RuntimeError."""
    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "speed": speed,
        },
    }
    resp, _, err = await _try_with_keys(f"text-to-speech/{voice_id}", json=payload, timeout=ELEVENLABS_TTS_TIMEOUT)
    if resp and resp.status == 200:
        audio = await _safe_resp_read(resp, timeout=ELEVENLABS_TTS_TIMEOUT)
        if audio:
            return audio
        raise RuntimeError("ElevenLabs TTS: Connection closed при чтении аудио")
    body = await _safe_resp_text(resp) if resp else ""
    msg = f"HTTP {resp.status}: {body[:200]}" if resp else (err or "неизвестно")
    logger.error(f"ElevenLabs TTS failed: {msg}")
    raise RuntimeError(f"ElevenLabs TTS: {msg}")


# ── Voice Cloning ───────────────────────────────────────────────────────────

async def elevenlabs_add_voice(name: str, audio_bytes: bytes, description: str = "") -> str:
    """Clone a voice from audio or raise with the provider's diagnostic."""
    form = aiohttp.FormData()
    form.add_field("name", name)
    form.add_field("description", description or f"Voice clone: {name}")
    form.add_field("files", audio_bytes, filename="sample.ogg", content_type="audio/ogg")
    form.add_field("remove_background_noise", "true")
    response, key_info, error = await _try_with_keys("voices/add", method="POST", data=form, timeout=ELEVENLABS_CLONE_TIMEOUT)
    if response and response.status == 200:
        data = await _safe_resp_json(response)
        voice_id = data.get("voice_id", "") if data else ""
        if voice_id:
            logger.info(f"ElevenLabs voice cloned: {name} → {voice_id} (tier: {key_info.get('tier', '?') if key_info else '?'})")
            return voice_id
        error = "successful response contained no voice_id"
    elif response:
        body = await _safe_resp_text(response)
        error = f"HTTP {response.status}: {body[:300]}"
    message = error or "provider returned no response"
    logger.error(f"ElevenLabs add_voice failed: {message}")
    raise RuntimeError(f"ElevenLabs add_voice: {message}")



async def elevenlabs_delete_voice(voice_id: str) -> bool:
    """Delete a cloned voice or raise with the provider's diagnostic."""
    response, _, error = await _try_with_keys(f"voices/{voice_id}", method="DELETE")
    if response and response.status == 200:
        logger.info(f"ElevenLabs voice deleted: {voice_id}")
        return True
    if response:
        body = await _safe_resp_text(response)
        error = f"HTTP {response.status}: {body[:300]}"
    if error and "HTTP 404" in error:
        logger.info(f"ElevenLabs voice already absent: {voice_id} ({error})")
        return True
    message = error or "ElevenLabs returned no response"
    logger.error(f"ElevenLabs voice delete failed: {message}")
    raise RuntimeError(f"ElevenLabs voice delete failed: {message}")


async def elevenlabs_get_voice(voice_id: str) -> Optional[dict]:
    """Get voice metadata by ID."""
    resp, _, _ = await _try_with_keys(f"voices/{voice_id}", method="GET")
    if resp and resp.status == 200:
        return await _safe_resp_json(resp)
    return None


# ── Music ───────────────────────────────────────────────────────────────────

async def elevenlabs_music(prompt: str, duration_ms: int = ELEVENLABS_MUSIC_DURATION_MS, model: str = ELEVENLABS_MUSIC_MODEL) -> bytes:
    """Generate music or raise with the provider's diagnostic."""
    payload = {"prompt": prompt, "music_length_ms": duration_ms, "model_id": model}
    response, _, error = await _try_with_keys("music/compose", json=payload, timeout=ELEVENLABS_MUSIC_TIMEOUT)
    return await _require_audio_response("ElevenLabs music", response, error, ELEVENLABS_MUSIC_TIMEOUT)


# ── Sound Effects ───────────────────────────────────────────────────────────

async def elevenlabs_sfx(text: str, duration_seconds: float = 5.0, model: str = ELEVENLABS_SFX_MODEL) -> bytes:
    """Generate a sound effect or raise with the provider's diagnostic."""
    payload = {"text": text, "duration_seconds": duration_seconds, "model_id": model}
    response, _, error = await _try_with_keys("text-to-sound-effects/convert", json=payload, timeout=ELEVENLABS_SFX_TIMEOUT)
    return await _require_audio_response("ElevenLabs SFX", response, error, 120)


# ── Voice Isolator ──────────────────────────────────────────────────────────

async def elevenlabs_voice_isolator(audio_bytes: bytes) -> bytes:
    """Remove background noise or raise with the provider's diagnostic."""
    form = aiohttp.FormData()
    form.add_field("audio", audio_bytes, filename="input.mp3", content_type="audio/mpeg")
    response, _, error = await _try_with_keys("audio-isolation", method="POST", data=form, timeout=ELEVENLABS_ISOLATOR_TIMEOUT)
    return await _require_audio_response("ElevenLabs voice isolator", response, error, 240)


# ── Voice Changer ───────────────────────────────────────────────────────────

async def elevenlabs_voice_changer(audio_bytes: bytes, voice_id: str, model: str = ELEVENLABS_VOICE_CHANGER_MODEL,
                                    remove_noise: bool = True) -> tuple[Optional[bytes], Optional[str]]:
    """Transform audio to target voice preserving emotion/timing.
    Endpoint: POST /v1/speech-to-speech/{voice_id}.
    Returns (audio_bytes, error)."""
    form = aiohttp.FormData()
    form.add_field("audio", audio_bytes, filename="input.mp3", content_type="audio/mpeg")
    form.add_field("model_id", model)
    if remove_noise:
        form.add_field("remove_background_noise", "true")
    resp, ki, err = await _try_with_keys(f"speech-to-speech/{voice_id}", method="POST", data=form, timeout=ELEVENLABS_VOICE_CHANGER_TIMEOUT)
    if resp and resp.status == 200:
        result = await _safe_resp_read(resp, timeout=60)
        if result:
            logger.info(f"Voice changer: {voice_id} (tier: {ki.get('tier', '?') if ki else '?'})")
            return result, None
        return None, "Connection closed при чтении аудио"
    if resp:
        body = await _safe_resp_text(resp)
        msg = f"HTTP {resp.status}: {body[:200]}"
        logger.error(f"ElevenLabs voice changer failed: {msg}")
        return None, msg
    return None, err or "неизвестная ошибка"

async def elevenlabs_stt(audio_bytes: bytes, language: str = None) -> tuple[Optional[str], Optional[str]]:
    """Transcribe audio to text using Scribe v2. 90+ langs, speaker diarization.
    Returns (text, error)."""
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename="audio.mp3", content_type="audio/mpeg")
    form.add_field("model_id", ELEVENLABS_STT_MODEL)
    if language:
        form.add_field("language_code", language)
    resp, _, err = await _try_with_keys("speech-to-text", method="POST", data=form, timeout=ELEVENLABS_STT_TIMEOUT)
    if resp and resp.status == 200:
        data = await _safe_resp_json(resp)
        if data:
            return data.get("text", ""), None
        return None, "пустой ответ от API"
    if resp:
        body = await _safe_resp_text(resp)
        msg = f"HTTP {resp.status}: {body[:200]}"
        logger.error(f"ElevenLabs STT failed: {msg}")
        return None, msg
    return None, err or "неизвестная ошибка"


# ── Voice Design (generate new voices from prompt) ──────────────────────────

async def elevenlabs_design_voice(name: str, prompt: str, description: str = "") -> str:
    """Generate a new voice or raise with the provider's diagnostic."""
    payload = {
        "name": name,
        "text": prompt,
        "description": description or f"Generated voice: {name}",
    }
    response, _, error = await _try_with_keys("voice-generation/generate-voice", json=payload, timeout=ELEVENLABS_VOICE_DESIGN_TIMEOUT)
    if response and response.status == 200:
        data = await _safe_resp_json(response)
        voice_id = data.get("voice_id", "") if data else ""
        if voice_id:
            return voice_id
        error = "successful response contained no voice_id"
    elif response:
        body = await _safe_resp_text(response)
        error = f"HTTP {response.status}: {body[:300]}"
    message = error or "provider returned no response"
    logger.error(f"ElevenLabs voice design failed: {message}")
    raise RuntimeError(f"ElevenLabs voice design: {message}")

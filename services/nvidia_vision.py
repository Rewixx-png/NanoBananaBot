"""NVIDIA Vision service — image/video analysis via Llama 3.2 90B Vision Instruct.

Free tier via NVIDIA NIM API. Keys from RewTest keyhunter,
validated on every TTL cycle. Only verified working keys are used.

Public API:
  analyze_image(image_bytes, prompt) -> str
  analyze_video(video_bytes, prompt, max_frames=8) -> str
  extract_video_frames(video_bytes, max_frames=8) -> list[bytes]
"""

import asyncio
import base64
import logging
import os
import tempfile
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "meta/llama-3.2-90b-vision-instruct"

# --- Key cache with validation ---

_validated_keys: list[str] = []
_validated_at: float = 0.0
_VALIDATION_TTL = 300  # seconds


async def _get_validated_keys() -> list[str]:
    """Scan keyhunter, validate every key with /models endpoint, cache."""
    import time as _time
    global _validated_keys, _validated_at
    now = _time.time()
    if _validated_keys and now - _validated_at < _VALIDATION_TTL:
        return _validated_keys

    import aiosqlite
    from keys.manager import REWTEST_DB
    try:
        async with aiosqlite.connect(REWTEST_DB, timeout=3) as db:
            async with db.execute(
                "SELECT key FROM keys WHERE service='Nvidia' AND is_live=1"
            ) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        logger.warning(f"NVIDIA: keyhunter query failed: {e}")
        return _validated_keys  # stale cache

    all_keys = [r[0] for r in rows if r[0] and "test" not in r[0].lower()]
    logger.info(f"NVIDIA: loaded {len(all_keys)} keys, validating...")

    async def _validate_one(key: str) -> bool:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{NVIDIA_BASE}/models",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    valid: list[str] = []
    batch_size = 10
    for i in range(0, len(all_keys), batch_size):
        batch = all_keys[i : i + batch_size]
        results = await asyncio.gather(
            *[_validate_one(k) for k in batch], return_exceptions=True
        )
        for k, r in zip(batch, results):
            if r is True:
                valid.append(k)

    _validated_keys = valid
    _validated_at = now
    logger.info(f"NVIDIA: {len(valid)}/{len(all_keys)} keys valid")
    return valid


async def _nvidia_request(
    messages: list[dict],
    max_tokens: int = 600,
    timeout: int = 60,
) -> Optional[str]:
    """Send chat completion request cycling through validated keys."""
    keys = await _get_validated_keys()
    if not keys:
        logger.warning("NVIDIA: no validated keys available")
        return None

    payload = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    timeout_obj = aiohttp.ClientTimeout(total=timeout)

    for key in keys[:10]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{NVIDIA_BASE}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    timeout=timeout_obj,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
                    if resp.status in (401, 403, 429):
                        continue
        except Exception as e:
            logger.debug(f"NVIDIA: key failed: {type(e).__name__}: {e}")
            continue
    return None


# --- Public API ---


def image_bytes_to_base64(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    """Convert image bytes to base64 data URI."""
    b64 = base64.b64encode(image_bytes).decode()
    return f"data:{mime};base64,{b64}"


async def analyze_image(
    image_bytes: bytes,
    prompt: str = "Что на этом изображении? Опиши подробно.",
) -> Optional[str]:
    """Analyze a single image with NVIDIA 90B Vision."""
    data_uri = image_bytes_to_base64(image_bytes)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    ]
    return await _nvidia_request(messages, max_tokens=600, timeout=60)


async def analyze_video(
    video_bytes: bytes,
    prompt: str = "Проанализируй эти кадры видео. Что на них происходит? Опиши сюжет, действия, ключевые детали.",
    max_frames: int = 8,
) -> Optional[str]:
    """Extract frames from video and analyze with NVIDIA 90B Vision."""
    frames = extract_video_frames(video_bytes, max_frames)
    if not frames:
        return "Не удалось извлечь кадры из видео."

    content = [{"type": "text", "text": prompt}]
    for frame in frames[:max_frames]:
        data_uri = image_bytes_to_base64(frame)
        content.append({"type": "image_url", "image_url": {"url": data_uri}})

    messages = [{"role": "user", "content": content}]
    return await _nvidia_request(messages, max_tokens=800, timeout=90)


def extract_video_frames(video_bytes: bytes, max_frames: int = 8) -> list[bytes]:
    """Extract evenly-spaced JPEG frames from video via ffmpeg."""
    import subprocess

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(video_bytes)
        in_path = f.name

    out_dir = tempfile.mkdtemp(prefix="nv_frames_")
    try:
        dur_proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", in_path,
            ],
            capture_output=True, text=True, timeout=10,
        )
        duration = float(dur_proc.stdout.strip()) if dur_proc.stdout.strip() else 10.0

        interval = max(1.0, duration / max_frames)
        frame_paths: list[str] = []
        for i in range(max_frames):
            ts = i * interval
            out_path = os.path.join(out_dir, f"frame_{i:03d}.jpg")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", str(ts), "-i", in_path,
                    "-vframes", "1", "-q:v", "2", out_path,
                ],
                capture_output=True, timeout=15,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
                frame_paths.append(out_path)

        frames: list[bytes] = []
        for fp in frame_paths[:max_frames]:
            with open(fp, "rb") as fh:
                frames.append(fh.read())
        return frames
    finally:
        os.unlink(in_path)
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)

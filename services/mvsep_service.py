"""MVSEP API service — free music separation via mvsep.com.

API docs: https://mvsep.com/en/full_api
sep_type=40: BS Roformer (vocals, instrumental) — SDR 11.89
"""

import asyncio
import logging
from typing import Optional

import aiohttp
from config import MVSEP_API_TOKEN, MVSEP_BASE_URL

logger = logging.getLogger(__name__)



async def mvsep_separate(audio_bytes: bytes, sep_type: int = 40) -> tuple[Optional[dict], Optional[str]]:
    """Upload audio to MVSEP, poll until done.
    Returns ({"vocal_url": str, "inst_url": str, "hash": str}, None) on success,
    or (None, error_message) on failure."""
    if not MVSEP_API_TOKEN:
        return None, "MVSEP_API_TOKEN не задан в окружении. Добавьте его в .env."
    async with aiohttp.ClientSession() as session:
        # Step 1: Create separation
        data = aiohttp.FormData()
        data.add_field("api_token", MVSEP_API_TOKEN)
        data.add_field("sep_type", str(sep_type))
        data.add_field("output_format", "1")  # wav
        data.add_field("audiofile", audio_bytes, filename="input.mp3", content_type="audio/mpeg")

        try:
            async with session.post(f"{MVSEP_BASE_URL}/separation/create", data=data,
                                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    err = f"HTTP {resp.status}: {body[:200]}"
                    logger.error(f"MVSEP create: {err}")
                    return None, err
                result = await resp.json()
        except asyncio.TimeoutError:
            return None, "таймаут загрузки на MVSEP (сервер не отвечает)"

        if not result.get("success"):
            msg = result.get("data", {}).get("message", "unknown")
            logger.error(f"MVSEP create error: {msg}")
            return None, msg

        job_hash = result["data"]["hash"]
        logger.info(f"MVSEP job created: {job_hash}")

        # Step 2: Poll until done
        for attempt in range(200):  # max ~10 min at 3s intervals
            await asyncio.sleep(3)
            try:
                async with session.get(
                    f"{MVSEP_BASE_URL}/separation/get", params={"hash": job_hash},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        continue
                    poll_data = await resp.json()
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"MVSEP poll {attempt}: {type(e).__name__}: {e}")
                continue

            status = poll_data.get("status", "")
            if status == "done":
                files = poll_data.get("data", {}).get("files", [])
                vocal_url = inst_url = None
                for f in files:
                    label = (f.get("name") or f.get("type") or "").lower()
                    url = f.get("url", "")
                    if "vocal" in label or "voice" in label:
                        vocal_url = url
                    elif "instrument" in label or "no_vocal" in label or "inst" in label:
                        inst_url = url
                # Fallback: first file = vocals, second = instrumental (BS Roformer order)
                if not vocal_url and len(files) >= 1:
                    vocal_url = files[0].get("url", "")
                if not inst_url and len(files) >= 2:
                    inst_url = files[1].get("url", "")
                if vocal_url and inst_url:
                    logger.info(f"MVSEP done: {job_hash} (attempt {attempt})")
                    return {"vocal_url": vocal_url, "inst_url": inst_url, "hash": job_hash}, None
                return None, f"URLs not found in {len(files)} files: {[f.get('type','?') for f in files]}"
            elif status == "failed":
                msg = poll_data.get("data", {}).get("message", "unknown")
                logger.error(f"MVSEP failed: {msg}")
                return None, msg

        return None, "timeout (10 min)"


async def mvsep_download(url: str) -> Optional[bytes]:
    """Download a file from MVSEP result URL."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status == 200:
                return await resp.read()
            logger.error(f"MVSEP download failed: HTTP {resp.status}")
            return None

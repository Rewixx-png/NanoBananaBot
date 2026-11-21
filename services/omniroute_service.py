"""OmniRoute OpenAI-compatible gateway client for Agent orchestration and Vision.

Uses OMNIROUTE_BASE_URL, OMNIROUTE_API_KEY, OMNIROUTE_AGENT_MODEL, and
OMNIROUTE_VISION_MODEL from config.py (loaded from .env).
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Optional

import aiohttp

from config import (
    OMNIROUTE_AGENT_MODEL,
    OMNIROUTE_API_KEY,
    OMNIROUTE_APP_TITLE,
    OMNIROUTE_BASE_URL,
    OMNIROUTE_TIMEOUT,
    OMNIROUTE_VISION_MODEL,
    PHOTO_ANALYSIS_MAX_TOKENS,
    PHOTO_ANALYSIS_TIMEOUT,
    VIDEO_VISION_MAX_FRAMES,
)
from shared_types import _guess_image_mime

logger = logging.getLogger(__name__)

_call_counter: int = 0


def is_omniroute_configured() -> bool:
    """Return True when OmniRoute Base URL and API key are configured."""
    return bool(OMNIROUTE_BASE_URL and OMNIROUTE_API_KEY)


def _omniroute_headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {OMNIROUTE_API_KEY}",
        "Content-Type": "application/json",
    }
    if OMNIROUTE_APP_TITLE:
        headers["X-Title"] = OMNIROUTE_APP_TITLE
    return headers


def gemini_contents_to_openai_messages(contents: list) -> list[dict]:
    """Convert internal Gemini-style agent contents into OpenAI chat messages."""
    global _call_counter
    messages: list[dict] = []
    for content in contents:
        role = content.get("role", "user")
        if role == "model":
            role = "assistant"
        parts = content.get("parts", [])
        text_parts = [p.get("text", "") for p in parts if "text" in p and p.get("text")]
        fc_parts = [p.get("functionCall") for p in parts if "functionCall" in p]
        fr_parts = [p.get("functionResponse") for p in parts if "functionResponse" in p]

        if fc_parts:
            tool_calls = []
            for fc in fc_parts:
                _call_counter += 1
                tool_calls.append({
                    "id": f"call_{_call_counter}",
                    "type": "function",
                    "function": {
                        "name": fc["name"],
                        "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False),
                    },
                })
            messages.append({
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
                "tool_calls": tool_calls,
            })
        if fr_parts:
            call_ids: list[str] = []
            for prev in reversed(messages):
                if prev.get("role") == "assistant" and prev.get("tool_calls"):
                    call_ids = [tc["id"] for tc in prev["tool_calls"]]
                    break
            for idx, fr in enumerate(fr_parts):
                call_id = call_ids[idx] if idx < len(call_ids) else f"call_{idx}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(fr.get("response", {}).get("result", "")),
                })
        if not fc_parts and not fr_parts and text_parts:
            messages.append({"role": role, "content": "\n".join(text_parts)})
    return messages


def gemini_tools_to_openai(tools: list) -> list[dict]:
    """Convert tool declarations (flat or wrapped) into OpenAI tool definitions."""
    result: list[dict] = []
    for tool in tools:
        for fd in tool.get("functionDeclarations", []):
            result.append({
                "type": "function",
                "function": {
                    "name": fd["name"],
                    "description": fd.get("description", ""),
                    "parameters": fd.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        if "name" in tool and "parameters" in tool:
            result.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            })
    return result


def openai_response_to_gemini(data: dict) -> dict:
    """Convert OpenAI chat completion JSON into the agent loop's internal candidate format."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish = choice.get("finish_reason", "stop")

    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        parts = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments", "{}")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args or "{}")
                except Exception:
                    args = {}
            parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
        return {"content": {"parts": parts, "role": "model"}, "_finish": finish}

    content = msg.get("content", "") or ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return {"content": {"parts": [{"text": content}], "role": "model"}, "_finish": finish}


def _extract_response_text(data: dict, model: str) -> str:
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    content = (content or "").strip()
    if not content:
        raise RuntimeError(f"OmniRoute {model}: успешный ответ не содержит текста")
    return content


async def fetch_omniroute_models(timeout: int = 20) -> list[dict[str, Any]]:
    """Query GET /models on the configured OmniRoute gateway."""
    if not is_omniroute_configured():
        return []
    from utils import get_http_session
    session = await get_http_session()
    try:
        async with session.get(
            f"{OMNIROUTE_BASE_URL}/models",
            headers=_omniroute_headers(),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                items = data.get("data", []) if isinstance(data, dict) else []
                return [item for item in items if isinstance(item, dict) and item.get("id")]
            logger.warning("OmniRoute /models HTTP %s", resp.status)
    except Exception as exc:
        logger.warning("OmniRoute /models failed: %s: %s", type(exc).__name__, exc)
    return []


async def omniroute_chat(
    messages: list[dict],
    system_prompt: str = "",
    model: str = "",
    max_tokens: int = 4000,
    tools: Optional[list[dict]] = None,
    temperature: Optional[float] = None,
    timeout: Optional[int] = None,
    retries: int = 2,
) -> dict:
    """Send an OpenAI-compatible chat completion request to OmniRoute."""
    if not OMNIROUTE_BASE_URL:
        raise RuntimeError("OmniRoute: OMNIROUTE_BASE_URL не задан в .env")
    if not OMNIROUTE_API_KEY:
        raise RuntimeError("OmniRoute: OMNIROUTE_API_KEY не задан в .env")

    effective_model = (model or OMNIROUTE_AGENT_MODEL).strip()
    effective_timeout = timeout if timeout is not None else OMNIROUTE_TIMEOUT
    request_messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + list(messages)

    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": request_messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    from utils import get_http_session
    session = await get_http_session()
    url = f"{OMNIROUTE_BASE_URL}/chat/completions"
    headers = _omniroute_headers()
    req_timeout = aiohttp.ClientTimeout(total=effective_timeout)
    last_error = "provider returned no response"

    for attempt in range(max(1, retries + 1)):
        try:
            async with session.post(url, json=payload, headers=headers, timeout=req_timeout) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("choices"):
                        return data
                    last_error = "успешный ответ не содержит choices"
                else:
                    body = await resp.text()
                    last_error = f"HTTP {resp.status}: {body[:500]}"
                    if resp.status in (400, 401, 403, 404):
                        break
        except asyncio.TimeoutError:
            last_error = f"таймаут после {effective_timeout} секунд"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < retries:
            await asyncio.sleep(min(2 * (attempt + 1), 5))

    raise RuntimeError(f"OmniRoute {effective_model}: {last_error}")


async def omniroute_text(
    prompt: str,
    system_prompt: str = "",
    model: str = "",
    max_tokens: int = 1000,
    temperature: Optional[float] = None,
    timeout: Optional[int] = None,
) -> str:
    """Generate plain text through OmniRoute."""
    effective_model = (model or OMNIROUTE_AGENT_MODEL).strip()
    data = await omniroute_chat(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=system_prompt,
        model=effective_model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )
    try:
        return _extract_response_text(data, effective_model)
    except RuntimeError:
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length" and max_tokens < 4096:
            retry_tokens = min(max(max_tokens * 2, 1024), 4096)
            data = await omniroute_chat(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=system_prompt,
                model=effective_model,
                max_tokens=retry_tokens,
                temperature=temperature,
                timeout=timeout,
            )
            return _extract_response_text(data, effective_model)
        raise


async def analyze_images_with_omniroute(
    images_bytes: list[bytes],
    prompt: str = "",
    model: str = "",
    system_prompt: str = "",
    max_tokens: Optional[int] = None,
    timeout: Optional[int] = None,
) -> str:
    """Analyze one or more local images through the configured OmniRoute vision model."""
    valid_images = [img for img in images_bytes if img]
    if not valid_images:
        raise ValueError("OmniRoute vision: не передано ни одного изображения")

    effective_model = (model or OMNIROUTE_VISION_MODEL).strip()
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt or "Что на этом фото? Опиши подробно."}
    ]
    for img in valid_images:
        mime = _guess_image_mime(img)
        b64 = base64.b64encode(img).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })

    data = await omniroute_chat(
        messages=[{"role": "user", "content": content}],
        system_prompt=system_prompt,
        model=effective_model,
        max_tokens=max_tokens if max_tokens is not None else PHOTO_ANALYSIS_MAX_TOKENS,
        timeout=timeout if timeout is not None else PHOTO_ANALYSIS_TIMEOUT,
    )
    return _extract_response_text(data, effective_model)


async def analyze_image_with_omniroute(
    image_bytes: bytes,
    prompt: str = "",
    model: str = "",
    system_prompt: str = "",
    max_tokens: Optional[int] = None,
    timeout: Optional[int] = None,
) -> str:
    """Analyze a single local image through the configured OmniRoute vision model."""
    return await analyze_images_with_omniroute(
        [image_bytes],
        prompt=prompt,
        model=model,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        timeout=timeout,
    )


async def extract_video_frames(video_bytes: bytes, max_frames: int = VIDEO_VISION_MAX_FRAMES) -> list[bytes]:
    """Extract evenly-spaced JPEG frames from video bytes via ffmpeg."""
    from utils import run_ffmpeg

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(video_bytes)
        in_path = f.name

    out_dir = tempfile.mkdtemp(prefix="omni_frames_")
    try:
        try:
            _, stdout, _ = await run_ffmpeg(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "csv=p=0", in_path,
                ],
                timeout=10,
            )
            duration = float(stdout.strip()) if stdout.strip() else 10.0
        except Exception:
            duration = 10.0

        interval = max(1.0, duration / max(1, max_frames))
        frame_paths: list[str] = []
        for idx in range(max_frames):
            ts = idx * interval
            out_path = os.path.join(out_dir, f"frame_{idx:03d}.jpg")
            try:
                await run_ffmpeg(
                    [
                        "ffmpeg", "-y", "-ss", str(ts), "-i", in_path,
                        "-vframes", "1", "-q:v", "2", out_path,
                    ],
                    timeout=15,
                )
            except Exception:
                pass
            if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
                frame_paths.append(out_path)

        frames: list[bytes] = []
        for fp in frame_paths[:max_frames]:
            with open(fp, "rb") as fh:
                frames.append(fh.read())
        return frames
    finally:
        if os.path.exists(in_path):
            os.unlink(in_path)
        shutil.rmtree(out_dir, ignore_errors=True)


async def analyze_video_with_omniroute(
    video_bytes: bytes,
    prompt: str = "Проанализируй эти кадры видео. Что на них происходит? Опиши сюжет, действия, ключевые детали.",
    max_frames: int = VIDEO_VISION_MAX_FRAMES,
) -> str:
    """Extract frames from video and analyze them through OmniRoute Vision."""
    frames = await extract_video_frames(video_bytes, max_frames=max_frames)
    if not frames:
        return "Не удалось извлечь кадры из видео."
    return await analyze_images_with_omniroute(
        frames,
        prompt=prompt,
        max_tokens=PHOTO_ANALYSIS_MAX_TOKENS,
        timeout=PHOTO_ANALYSIS_TIMEOUT,
    )

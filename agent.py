"""
Agentic loop for NanoHatani bot — 16 tools.
ReAct pattern: think → [tools] → reply/generate_project
"""
import asyncio
import hashlib
import io
import json
import logging
import math
import operator
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import deque
from typing import Any, Callable, Optional, Tuple

import aiohttp

from ai_services import (
    generate_image_with_gemini,
    generate_project_with_gemini,
    generate_tts_with_gemini,
)
from keys_manager import load_keys, load_firecrawl_keys, remove_key

logger = logging.getLogger(__name__)

MAX_STEPS = 16
_SEARCH_TIMEOUT  = 20.0
_SCRAPE_TIMEOUT  = 25.0
_LLM_TIMEOUT     = 60.0
_PROJECT_TIMEOUT = 180.0
_VDL_TIMEOUT     = 120.0   # yt-dlp
_TG_MAX_BYTES    = 48 * 1024 * 1024   # 48 MB Telegram upload limit


# ── Loop safety ──────────────────────────────────────────────────

class _DebounceHook:
    def __init__(self, window: int = 6, max_repeats: int = 2):
        self._win: deque[str] = deque(maxlen=window)
        self._max = max_repeats

    def check(self, name: str, args: dict) -> Optional[str]:
        fp = hashlib.md5(f"{name}:{sorted(args.items())}".encode()).hexdigest()
        if sum(1 for f in self._win if f == fp) >= self._max:
            return (
                f"LOOP DETECTED: '{name}' called with identical args {self._max}+ times. "
                "Change your approach."
            )
        self._win.append(fp)
        return None


class _ToolBudget:
    LIMITS = {
        "web_search": 8, "scrape_url": 10, "generate_project": 2,
        "think": 30, "reply": 3, "generate_image": 3, "download_image": 5,
        "download_video": 2, "text_to_speech": 3, "run_python": 5,
        "fetch_json": 8, "calculate": 20, "qr_code": 3,
        "create_chart": 3, "translate": 5, "create_file": 5,
    }

    def __init__(self):
        self._counts: dict[str, int] = {}

    def charge(self, name: str) -> Optional[str]:
        limit = self.LIMITS.get(name, 20)
        count = self._counts.get(name, 0) + 1
        self._counts[name] = count
        if count > limit:
            return f"BUDGET: '{name}' exceeded limit {limit}. Use a different tool."
        return None


# ── Tool implementations ─────────────────────────────────────────

async def _fc_search(query: str) -> str:
    keys = load_firecrawl_keys()
    if not keys:
        return "Firecrawl keys unavailable."
    payload = {
        "query": query[:500], "limit": 6,
        "sources": [{"type": "web"}],
        "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
    }
    for key in keys:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/search",
                    json=payload, headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=_SEARCH_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("data", [])
                        if isinstance(results, dict):
                            results = results.get("results", []) or results.get("web", [])
                        parts = []
                        for r in results[:6]:
                            title = r.get("title") or r.get("metadata", {}).get("title", "")
                            url   = r.get("url", "")
                            body  = (r.get("markdown") or r.get("description") or "")[:800]
                            if url:
                                parts.append(f"### {title}\nURL: {url}\n{body}".strip())
                        return "\n\n".join(parts) or "No results."
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
        except Exception as e:
            logger.warning(f"web_search {query!r}: {e}")
    return "Search unavailable."


async def _fc_scrape(url: str) -> str:
    keys = load_firecrawl_keys()
    if not keys:
        return "Firecrawl keys unavailable."
    payload = {"url": url, "formats": ["markdown"], "onlyMainContent": True,
               "removeBase64Images": True, "maxAge": 3_600_000}
    for key in keys:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/scrape",
                    json=payload, headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=_SCRAPE_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        d = (await resp.json()).get("data") or {}
                        return (d.get("markdown") or d.get("content") or "Page empty.")[:8000]
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
        except Exception as e:
            logger.warning(f"scrape {url!r}: {e}")
    return "Could not read page."


async def _tool_generate_image(prompt: str, send_cb: Callable) -> str:
    img_bytes, err = await generate_image_with_gemini(prompt)
    if err or not img_bytes:
        return f"Image generation failed: {err or 'no data'}"
    await send_cb({"type": "photo", "data": img_bytes,
                   "caption": f"🎨 {prompt[:900]}", "filename": "image.jpg"})
    return "Image generated and sent to chat."


async def _tool_download_image(url: str, caption: str, send_cb: Callable) -> str:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return f"HTTP {resp.status} downloading image."
                data = await resp.read()
        if len(data) > _TG_MAX_BYTES:
            return f"Image too large ({len(data) // 1024} KB > 48 MB)."
        ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
        fname = f"image.{ext}" if ext in ("jpg", "jpeg", "png", "webp", "gif") else "image.jpg"
        await send_cb({"type": "photo", "data": data,
                       "caption": caption[:1024] or url[:200], "filename": fname})
        return "Image downloaded and sent."
    except Exception as e:
        return f"Failed to download image: {e}"


async def _tool_download_video(url: str, caption: str, send_cb: Callable) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        out_tmpl = os.path.join(tmpdir, "video.%(ext)s")
        cmd = [
            "yt-dlp", "--no-playlist", "--no-warnings",
            "-f", "bestvideo[height<=720][filesize<45M]+bestaudio/best[height<=720]/best[height<=480]",
            "--merge-output-format", "mp4",
            "-o", out_tmpl, url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_VDL_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                return "Video download timed out (2 min limit)."

            if proc.returncode != 0:
                err = stderr.decode()[:300]
                return f"yt-dlp error: {err}"

            # Find the downloaded file
            files = [f for f in os.listdir(tmpdir) if f.startswith("video.")]
            if not files:
                return "yt-dlp finished but no file found."

            fpath = os.path.join(tmpdir, files[0])
            size  = os.path.getsize(fpath)
            if size > _TG_MAX_BYTES:
                return f"Video too large ({size // 1024 // 1024} MB > 48 MB). Try a shorter clip."

            with open(fpath, "rb") as f:
                data = f.read()

            await send_cb({"type": "video", "data": data,
                           "caption": caption[:1024] or "📹 Видео",
                           "filename": files[0]})
            return f"Video downloaded ({size // 1024 // 1024} MB) and sent."

        except FileNotFoundError:
            return "yt-dlp not installed."
        except Exception as e:
            return f"Video download failed: {e}"


async def _tool_tts(text: str, voice: str, language: str, send_cb: Callable) -> str:
    voice    = voice    or "Kore"
    language = language or "ru-RU"
    audio, err = await generate_tts_with_gemini(
        text, model="gemini-3.5-flash-tts", voice_name=voice, language_code=language
    )
    if err or not audio:
        return f"TTS failed: {err or 'no audio'}"
    await send_cb({"type": "audio", "data": audio,
                   "caption": f"🎙 {text[:200]}", "filename": "speech.ogg"})
    return "Audio generated and sent."


def _run_python_sync(code: str) -> str:
    """Execute Python code in sandbox; return stdout (max 3000 chars)."""
    buf = io.StringIO()
    allowed_builtins = {
        "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
        "chr": chr, "dict": dict, "dir": dir, "divmod": divmod,
        "enumerate": enumerate, "filter": filter, "float": float,
        "format": format, "frozenset": frozenset, "getattr": getattr,
        "hasattr": hasattr, "hash": hash, "hex": hex, "int": int,
        "isinstance": isinstance, "issubclass": issubclass, "iter": iter,
        "len": len, "list": list, "map": map, "max": max, "min": min,
        "next": next, "oct": oct, "ord": ord, "pow": pow, "print": lambda *a, **k: print(*a, **k, file=buf),
        "range": range, "repr": repr, "reversed": reversed, "round": round,
        "set": set, "slice": slice, "sorted": sorted, "str": str,
        "sum": sum, "tuple": tuple, "type": type, "zip": zip,
        "True": True, "False": False, "None": None,
    }
    sandbox = {
        "__builtins__": allowed_builtins,
        "math": math, "json": json, "re": re,
    }
    try:
        exec(compile(code, "<agent>", "exec"), sandbox)  # noqa: S102
        output = buf.getvalue()
        return output[:3000] if output else "(no output — use print() to show results)"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


async def _tool_run_python(code: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_python_sync, code)


async def _tool_fetch_json(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15),
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    return f"HTTP {resp.status}"
                data = await resp.json(content_type=None)
                text = json.dumps(data, ensure_ascii=False, indent=2)
                return text[:5000]
    except Exception as e:
        return f"fetch_json error: {e}"


def _safe_eval(expr: str) -> str:
    """Evaluate a math expression safely using AST — no eval/exec."""
    import ast as _ast

    _MATH_FUNCS = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    _SAFE_NAMES = {"abs": abs, "round": round, "min": min, "max": max,
                   "pow": pow, "sum": sum, "len": len, **_MATH_FUNCS}

    _SAFE_OPS = {
        _ast.Add: operator.add, _ast.Sub: operator.sub,
        _ast.Mult: operator.mul, _ast.Div: operator.truediv,
        _ast.FloorDiv: operator.floordiv, _ast.Mod: operator.mod,
        _ast.Pow: operator.pow, _ast.USub: operator.neg, _ast.UAdd: operator.pos,
    }

    def _eval_node(node):
        if isinstance(node, _ast.Expression):
            return _eval_node(node.body)
        if isinstance(node, _ast.Constant):
            if isinstance(node.value, (int, float, complex)):
                return node.value
            raise ValueError(f"Unsupported constant: {node.value!r}")
        if isinstance(node, _ast.BinOp):
            op = _SAFE_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"Unsupported operator: {node.op!r}")
            return op(_eval_node(node.left), _eval_node(node.right))
        if isinstance(node, _ast.UnaryOp):
            op = _SAFE_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"Unsupported unary: {node.op!r}")
            return op(_eval_node(node.operand))
        if isinstance(node, _ast.Call):
            if not isinstance(node.func, _ast.Name):
                raise ValueError("Only named functions allowed")
            fn = _SAFE_NAMES.get(node.func.id)
            if fn is None:
                raise ValueError(f"Unknown function: {node.func.id!r}")
            return fn(*[_eval_node(a) for a in node.args])
        if isinstance(node, _ast.Name):
            val = _SAFE_NAMES.get(node.id)
            if val is None:
                raise ValueError(f"Unknown name: {node.id!r}")
            return val
        raise ValueError(f"Unsupported expression type: {type(node).__name__}")

    try:
        tree = _ast.parse(expr.strip(), mode="eval")
        result = _eval_node(tree)
        return str(result)
    except Exception as e:
        return f"Error: {e}"


async def _tool_qr_code(text: str, caption: str, send_cb: Callable) -> str:
    try:
        import qrcode
        from PIL import Image as PILImage
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        await send_cb({"type": "photo", "data": data,
                       "caption": caption[:1024] or f"QR: {text[:200]}",
                       "filename": "qr.png"})
        return "QR code generated and sent."
    except Exception as e:
        return f"QR code failed: {e}"


async def _tool_create_chart(
    chart_type: str, title: str,
    labels: list, values: list,
    xlabel: str, ylabel: str,
    send_cb: Callable,
) -> str:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 6))
        ax.set_title(title or "Chart", fontsize=14, pad=12)

        ctype = (chart_type or "bar").lower()
        if ctype == "bar":
            ax.bar(range(len(values)), values, color="#4C9BE8")
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right")
        elif ctype == "line":
            ax.plot(range(len(values)), values, marker="o", color="#4C9BE8", linewidth=2)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right")
        elif ctype == "pie":
            ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
            ax.axis("equal")
        elif ctype == "scatter":
            ax.scatter(range(len(values)), values, color="#4C9BE8", s=80)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right")
        else:
            ax.bar(range(len(values)), values)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right")

        if xlabel:
            ax.set_xlabel(xlabel)
        if ylabel:
            ax.set_ylabel(ylabel)

        ax.grid(axis="y", linestyle="--", alpha=0.4)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="PNG", dpi=150)
        plt.close(fig)
        data = buf.getvalue()

        await send_cb({"type": "photo", "data": data,
                       "caption": f"📊 {title}", "filename": "chart.png"})
        return "Chart created and sent."
    except Exception as e:
        return f"Chart failed: {e}"


async def _tool_translate(text: str, target_lang: str) -> str:
    keys = load_keys()
    if not keys:
        return "No Gemini keys."
    payload = {
        "contents": [{"parts": [{"text":
            f"Translate the following text to {target_lang}. "
            f"Return ONLY the translation, nothing else:\n\n{text[:3000]}"
        }]}],
        "generationConfig": {"temperature": 0.1},
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                url, json=payload,
                headers={"Content-Type": "application/json", "x-goog-api-key": keys[0]},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return f"Translate API error: {resp.status}"
    except Exception as e:
        return f"Translate failed: {e}"


async def _tool_create_file(filename: str, content: str, caption: str, send_cb: Callable) -> str:
    data = content.encode("utf-8")
    if len(data) > _TG_MAX_BYTES:
        return f"File too large ({len(data) // 1024} KB)."
    await send_cb({"type": "document", "data": data,
                   "caption": caption[:1024] or filename,
                   "filename": filename or "file.txt"})
    return f"File '{filename}' sent."


# ── Tool declarations for Gemini ─────────────────────────────────

_TOOLS = [
    {
        "name": "think",
        "description": (
            "Internal reasoning scratchpad. Think about the task, plan next steps, "
            "analyze gathered info. ALWAYS use before first search and before generate_project. "
            "Invisible to user."
        ),
        "parameters": {"type": "object",
                       "properties": {"thought": {"type": "string"}},
                       "required": ["thought"]},
    },
    {
        "name": "web_search",
        "description": (
            "Search the internet via Firecrawl. Use multiple different queries to cover "
            "the topic from different angles. Do NOT repeat the same query twice."
        ),
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string",
                                                "description": "Precise search query (3-10 words)"}},
                       "required": ["query"]},
    },
    {
        "name": "scrape_url",
        "description": "Read the full content of a specific web page. Use after web_search.",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]},
    },
    {
        "name": "generate_project",
        "description": (
            "Generate a complete project (website, program, bot) and send as files. "
            "Include ALL gathered research in prompt. Minimum 300 chars."
        ),
        "parameters": {"type": "object",
                       "properties": {"prompt": {"type": "string"}},
                       "required": ["prompt"]},
    },
    {
        "name": "reply",
        "description": "Send a final text reply to the user. Use when no files/media needed.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    },
    {
        "name": "generate_image",
        "description": (
            "Generate an AI image from a text prompt and send it to the chat. "
            "Use when user asks to draw, create, visualize something."
        ),
        "parameters": {"type": "object",
                       "properties": {"prompt": {"type": "string",
                                                 "description": "Detailed image description in English"}},
                       "required": ["prompt"]},
    },
    {
        "name": "download_image",
        "description": "Download an image from a URL and send it to the chat.",
        "parameters": {"type": "object",
                       "properties": {
                           "url": {"type": "string", "description": "Direct image URL"},
                           "caption": {"type": "string", "description": "Caption for the image"},
                       },
                       "required": ["url"]},
    },
    {
        "name": "download_video",
        "description": (
            "Download a video from YouTube, TikTok, Instagram, Twitter/X, VK "
            "or any yt-dlp supported URL and send to chat. Max 48 MB / 720p."
        ),
        "parameters": {"type": "object",
                       "properties": {
                           "url": {"type": "string", "description": "Video page URL"},
                           "caption": {"type": "string"},
                       },
                       "required": ["url"]},
    },
    {
        "name": "text_to_speech",
        "description": "Convert text to speech audio and send as voice message.",
        "parameters": {"type": "object",
                       "properties": {
                           "text": {"type": "string"},
                           "voice": {"type": "string",
                                     "description": "Voice name, e.g. Kore, Aoede, Charon, Fenrir, Puck"},
                           "language": {"type": "string",
                                        "description": "Language code, e.g. ru-RU, en-US"},
                       },
                       "required": ["text"]},
    },
    {
        "name": "run_python",
        "description": (
            "Execute Python code in a sandboxed environment and return stdout output. "
            "Use for calculations, data processing, generating text, parsing data. "
            "Use print() to output results. Available: math, json, re."
        ),
        "parameters": {"type": "object",
                       "properties": {"code": {"type": "string",
                                               "description": "Python code to execute"}},
                       "required": ["code"]},
    },
    {
        "name": "fetch_json",
        "description": "Fetch JSON data from any URL or API endpoint via HTTP GET.",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]},
    },
    {
        "name": "calculate",
        "description": (
            "Evaluate a mathematical expression. Supports math functions (sin, cos, sqrt, log, etc.). "
            "Example: '2**10 + math.sqrt(144)'"
        ),
        "parameters": {"type": "object",
                       "properties": {"expression": {"type": "string"}},
                       "required": ["expression"]},
    },
    {
        "name": "qr_code",
        "description": "Generate a QR code for any text or URL and send as image.",
        "parameters": {"type": "object",
                       "properties": {
                           "text": {"type": "string", "description": "Text or URL to encode"},
                           "caption": {"type": "string"},
                       },
                       "required": ["text"]},
    },
    {
        "name": "create_chart",
        "description": (
            "Create a chart/graph from data and send as image. "
            "Types: bar, line, pie, scatter."
        ),
        "parameters": {"type": "object",
                       "properties": {
                           "chart_type": {"type": "string", "enum": ["bar", "line", "pie", "scatter"]},
                           "title": {"type": "string"},
                           "labels": {"type": "array", "items": {"type": "string"},
                                      "description": "Category labels"},
                           "values": {"type": "array", "items": {"type": "number"}},
                           "xlabel": {"type": "string"},
                           "ylabel": {"type": "string"},
                       },
                       "required": ["chart_type", "labels", "values"]},
    },
    {
        "name": "translate",
        "description": "Translate text to any language.",
        "parameters": {"type": "object",
                       "properties": {
                           "text": {"type": "string"},
                           "target_language": {"type": "string",
                                               "description": "Target language, e.g. English, Spanish, Japanese"},
                       },
                       "required": ["text", "target_language"]},
    },
    {
        "name": "create_file",
        "description": "Create a text or code file with given content and send as document.",
        "parameters": {"type": "object",
                       "properties": {
                           "filename": {"type": "string", "description": "Filename with extension"},
                           "content": {"type": "string", "description": "File content"},
                           "caption": {"type": "string"},
                       },
                       "required": ["filename", "content"]},
    },
]


# ── Gemini LLM call ──────────────────────────────────────────────

_SYSTEM = (
    "Ты — Hatani AI, мощный агент. Выполняешь задачи через инструменты.\n\n"
    "ПРОТОКОЛ:\n"
    "1. think → обдумай, составь план\n"
    "2. Используй нужные инструменты (можно несколько за шаг)\n"
    "3. think → синтезируй результаты\n"
    "4. reply или generate_project для финального результата\n\n"
    "ДОСТУПНЫЕ ИНСТРУМЕНТЫ:\n"
    "- Поиск: web_search, scrape_url, fetch_json\n"
    "- Медиа: generate_image, download_image, download_video, text_to_speech\n"
    "- Утилиты: run_python, calculate, qr_code, create_chart, translate, create_file\n"
    "- Выход: generate_project (код/проект файлами), reply (текст)\n\n"
    "Не повторяй одинаковые вызовы. Используй think перед generate_project."
)


async def _gemini_call(keys: list, contents: list) -> dict:
    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM}]},
        "contents": contents,
        "tools": [{"functionDeclarations": _TOOLS}],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {
            "temperature": 0.7,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    for key in keys:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key},
                    timeout=aiohttp.ClientTimeout(total=_LLM_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("candidates", [{}])[0]
                    if resp.status in (429, 403):
                        remove_key(key, resp.status)
                        continue
                    logger.warning(f"agent gemini {resp.status}: {(await resp.text())[:200]}")
        except Exception as e:
            logger.warning(f"agent gemini: {type(e).__name__}: {e}")
    return {}


# ── Execute one tool safely ──────────────────────────────────────

async def _execute_tool(
    name: str,
    args: dict,
    debounce: _DebounceHook,
    budget: _ToolBudget,
    status_cb: Callable,
    send_cb: Optional[Callable],
) -> Tuple[str, Optional[dict]]:
    """Returns (result_text, project_dict_or_None)."""

    async def _st(text: str):
        try:
            await status_cb(text)
        except Exception:
            pass

    async def _send(media: dict):
        if send_cb:
            try:
                await send_cb(media)
            except Exception as e:
                logger.warning(f"send_cb failed: {e}")

    if err := debounce.check(name, args):
        return err, None
    if err := budget.charge(name):
        return err, None

    # ── Core tools ──

    if name == "think":
        thought = args.get("thought", "")
        logger.info(f"[agent:think] {thought[:300]}")
        await _st(f"💭 {thought[:120]}...")
        return "ok", None

    if name == "web_search":
        query = args.get("query", "")
        await _st(f"🔎 Ищу: «{query[:80]}»")
        return await _fc_search(query), None

    if name == "scrape_url":
        await _st("📄 Читаю страницу...")
        return await _fc_scrape(args.get("url", "")), None

    if name == "generate_project":
        prompt = args.get("prompt", "")
        await _st("⚙️ Генерирую проект...")
        try:
            project = await asyncio.wait_for(
                generate_project_with_gemini(prompt), timeout=_PROJECT_TIMEOUT
            )
        except asyncio.TimeoutError:
            return "generate_project timed out.", None
        if project.get("ok"):
            return "__PROJECT_DONE__", project
        return f"Project gen failed: {project.get('error', '?')}", None

    if name == "reply":
        return args.get("text", "Done."), None

    # ── Image tools ──

    if name == "generate_image":
        prompt = args.get("prompt", "")
        await _st(f"🎨 Генерирую изображение...")
        return await _tool_generate_image(prompt, _send), None

    if name == "download_image":
        await _st("⬇️ Скачиваю изображение...")
        return await _tool_download_image(
            args.get("url", ""), args.get("caption", ""), _send
        ), None

    # ── Video ──

    if name == "download_video":
        url = args.get("url", "")
        await _st(f"📹 Скачиваю видео (до 2 мин)...")
        return await _tool_download_video(url, args.get("caption", ""), _send), None

    # ── Audio ──

    if name == "text_to_speech":
        await _st("🎙 Озвучиваю текст...")
        return await _tool_tts(
            args.get("text", ""),
            args.get("voice", "Kore"),
            args.get("language", "ru-RU"),
            _send,
        ), None

    # ── Code / data ──

    if name == "run_python":
        code = args.get("code", "")
        await _st("🐍 Выполняю код...")
        return await _tool_run_python(code), None

    if name == "fetch_json":
        await _st(f"🌐 Запрашиваю {args.get('url', '')[:60]}...")
        return await _tool_fetch_json(args.get("url", "")), None

    if name == "calculate":
        result = _safe_eval(args.get("expression", "0"))
        return f"Result: {result}", None

    # ── Utilities ──

    if name == "qr_code":
        await _st("🔲 Генерирую QR-код...")
        return await _tool_qr_code(args.get("text", ""), args.get("caption", ""), _send), None

    if name == "create_chart":
        await _st("📊 Создаю график...")
        return await _tool_create_chart(
            args.get("chart_type", "bar"),
            args.get("title", ""),
            args.get("labels", []),
            args.get("values", []),
            args.get("xlabel", ""),
            args.get("ylabel", ""),
            _send,
        ), None

    if name == "translate":
        await _st(f"🌍 Перевожу на {args.get('target_language', '?')}...")
        return await _tool_translate(
            args.get("text", ""), args.get("target_language", "English")
        ), None

    if name == "create_file":
        await _st(f"📄 Создаю файл {args.get('filename', '')}...")
        return await _tool_create_file(
            args.get("filename", "file.txt"),
            args.get("content", ""),
            args.get("caption", ""),
            _send,
        ), None

    return f"Unknown tool: {name}", None


# ── Public API ───────────────────────────────────────────────────

async def run_agent(
    task: str,
    chat_id: int,
    username: str,
    status_cb: Callable[[str], Any],
    send_media_cb: Optional[Callable] = None,
) -> Tuple[Optional[str], Optional[dict]]:
    """
    Agentic loop. Returns (text_reply, project_payload) — one of them set.
    Media is sent directly via send_media_cb during execution.
    """
    keys = load_keys()
    if not keys:
        return "Gemini keys are dead.", None

    debounce = _DebounceHook()
    budget   = _ToolBudget()
    contents: list = [
        {"role": "user", "parts": [{"text": f"Задача от {username}:\n{task}"}]}
    ]

    async def _st(text: str):
        try:
            await status_cb(text)
        except Exception:
            pass

    for step in range(MAX_STEPS):
        await _st(f"🤖 Шаг {step + 1}/{MAX_STEPS}...")

        if MAX_STEPS - step <= 3:
            contents.append({"role": "user", "parts": [{"text":
                f"\n[СИСТЕМА: осталось {MAX_STEPS - step} шагов. "
                "Завершай: вызови reply или generate_project.]"
            }]})

        candidate = await _gemini_call(keys, contents)
        if not candidate:
            return "Agent failed — Gemini did not respond.", None

        parts = candidate.get("content", {}).get("parts", [])
        if not parts:
            return "Agent returned empty response.", None

        contents.append({"role": "model", "parts": parts})
        fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if not fn_calls:
            text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
            return text or "Done.", None

        tool_responses: list = []
        for fn in fn_calls:
            name = fn.get("name", "")
            args = fn.get("args", {})

            result_text, project = await _execute_tool(
                name, args, debounce, budget, status_cb, send_media_cb
            )

            if name == "generate_project" and project is not None:
                return None, project
            if name == "reply":
                return result_text, None

            tool_responses.append({
                "functionResponse": {
                    "name": name,
                    "response": {"result": result_text},
                }
            })

        contents.append({"role": "user", "parts": tool_responses})

    return "Agent exhausted all steps.", None


def is_agent_task(prompt: str) -> bool:
    lower = prompt.lower()
    web = {
        "найди", "поищи", "загугли", "чекни", "чекнуть", "узнай", "разузнай",
        "в инете", "в интернете", "погугли", "research", "look up",
        "скачай", "download", "переведи", "translate", "посчитай", "calculate",
        "qr", "qr-код", "график", "chart", "озвучь", "прочитай вслух",
    }
    create = {
        "сделай", "создай", "напиши", "сгенерируй", "собери", "склепай",
        "сверстай", "сайт", "страницу", "программу", "скрипт", "бота",
        "приложение", "проект", "html", "python", "визуализацию", "дашборд",
        "файл", "архив", "нарисуй", "покажи", "пришли", "отправь",
    }
    return any(w in lower for w in web) or any(c in lower for c in create)

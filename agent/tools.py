"""Utility tools for the agent loop — fetch, math, logs, file I/O."""
import json
import math
import operator
import os
import shutil
import tempfile
from typing import Callable, TYPE_CHECKING

import aiohttp

from log_filter import mask_secrets
from services.security_utils import is_safe_url

if TYPE_CHECKING:
    from .workspace import AgentWorkspace

from config import BOT_LOG_PATH, TELEGRAM_MEDIA_MAX_BYTES
_TG_MAX_BYTES = TELEGRAM_MEDIA_MAX_BYTES


async def _tool_fetch_json(url: str) -> str:
    if not is_safe_url(url):
        return "fetch_json error: unsafe URL blocked"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15),
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    return f"HTTP {resp.status}"
                data = await resp.json(content_type=None)
                return json.dumps(data, ensure_ascii=False, indent=2)[:5000]
    except Exception as e:
        return f"fetch_json error: {e}"


def _ast_eval(expr: str) -> str:
    """Safe math evaluator — AST only, no eval/exec."""
    import ast as _ast
    _MF = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    _N = {"abs": abs, "round": round, "min": min, "max": max, "pow": pow, **_MF}
    _OPS = {
        _ast.Add: operator.add, _ast.Sub: operator.sub,
        _ast.Mult: operator.mul, _ast.Div: operator.truediv,
        _ast.FloorDiv: operator.floordiv, _ast.Mod: operator.mod,
        _ast.Pow: operator.pow, _ast.USub: operator.neg, _ast.UAdd: operator.pos,
    }

    def ev(node):
        if isinstance(node, _ast.Expression): return ev(node.body)
        if isinstance(node, _ast.Constant):
            if isinstance(node.value, (int, float, complex)): return node.value
            raise ValueError(f"Bad constant: {node.value!r}")
        if isinstance(node, _ast.BinOp):
            op = _OPS.get(type(node.op))
            if not op: raise ValueError(f"Bad op: {node.op!r}")
            return op(ev(node.left), ev(node.right))
        if isinstance(node, _ast.UnaryOp):
            op = _OPS.get(type(node.op))
            if not op: raise ValueError(f"Bad unary: {node.op!r}")
            return op(ev(node.operand))
        if isinstance(node, _ast.Call):
            if not isinstance(node.func, _ast.Name): raise ValueError("Only named funcs")
            fn = _N.get(node.func.id)
            if not fn: raise ValueError(f"Unknown: {node.func.id!r}")
            return fn(*[ev(a) for a in node.args])
        if isinstance(node, _ast.Name):
            v = _N.get(node.id)
            if v is None: raise ValueError(f"Unknown name: {node.id!r}")
            return v
        raise ValueError(f"Unsupported: {type(node).__name__}")

    try:
        return str(ev(_ast.parse(expr.strip(), mode="eval")))
    except Exception as e:
        return f"Error: {e}"


async def _tool_read_bot_logs(lines: int = 100) -> str:
    """Read the last N lines of the bot's own log file from the host (masked)."""
    log_path = BOT_LOG_PATH
    if not os.path.exists(log_path):
        return f"{os.path.basename(log_path)} not found."
    try:
        n = max(1, min(int(lines or 100), 1000))
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
        raw = "".join(all_lines[-n:])[-3000:]
        return mask_secrets(raw) if raw else "(empty)"
    except Exception as e:
        return f"Error reading bot.log: {e}"


_EXT_MEDIA_TYPES = {
    ".jpg": "photo",
    ".jpeg": "photo",
    ".png": "photo",
    ".webp": "photo",
    ".gif": "animation",
    ".mp4": "video",
    ".mov": "video",
    ".webm": "video",
    ".mkv": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".flac": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".ogg": "voice",
    ".opus": "voice",
}
_VALID_MEDIA_TYPES = {"document", "photo", "video", "audio", "voice", "animation"}


def _resolve_workspace_media_type(filename: str, media_type: str = "auto") -> str:
    requested = (media_type or "auto").strip().lower()
    if requested in _VALID_MEDIA_TYPES:
        return requested
    ext = os.path.splitext(filename.lower())[1]
    return _EXT_MEDIA_TYPES.get(ext, "document")


async def _tool_send_workspace_file(
    rel_path: str,
    caption: str,
    ws: "AgentWorkspace",
    send_cb: Callable,
    media_type: str = "auto",
) -> str:
    """Read a file (or directory -> zip) from workspace and send as document or media."""
    try:
        full = ws._safe_path(rel_path)
    except ValueError as e:
        return f"Access denied: {e}"
    if not os.path.exists(full):
        return f"File not found: {rel_path}"
    if os.path.isdir(full):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_base = os.path.join(tmpdir, "archive")
            archive_path = shutil.make_archive(archive_base, "zip", full)
            size = os.path.getsize(archive_path)
            if size > _TG_MAX_BYTES:
                return f"Directory too large ({size // 1024 // 1024} MB > {_TG_MAX_BYTES // 1024 // 1024} MB)."
            with open(archive_path, "rb") as f:
                data = f.read()
        filename = (os.path.basename(rel_path.rstrip("/")) or "workspace") + ".zip"
        await send_cb({"type": "document", "data": data,
                       "caption": (caption[:1024] or filename)[:1024], "filename": filename})
        return f"[ОТПРАВЛЕНО] Архив '{filename}' ({size // 1024} KB) отправлен."
    size = os.path.getsize(full)
    if size > _TG_MAX_BYTES:
        return f"File too large ({size // 1024 // 1024} MB > {_TG_MAX_BYTES // 1024 // 1024} MB)."
    with open(full, "rb") as f:
        data = f.read()
    filename = os.path.basename(rel_path)
    resolved_type = _resolve_workspace_media_type(filename, media_type)
    await send_cb({"type": resolved_type, "data": data,
                   "caption": caption[:1024] or filename, "filename": filename})
    return f"[ОТПРАВЛЕНО] '{filename}' ({size // 1024} KB, {resolved_type}) отправлен."


async def _tool_create_file(filename: str, content: str, caption: str, send_cb: Callable) -> str:
    data = content.encode("utf-8")
    if len(data) > _TG_MAX_BYTES:
        return f"File too large ({len(data) // 1024} KB)."
    await send_cb({"type": "document", "data": data,
                   "caption": caption[:1024] or filename, "filename": filename or "file.txt"})
    return f"File '{filename}' sent."

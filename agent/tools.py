"""Utility tools for the agent loop — fetch, math, logs, file I/O."""
import json
import math
import operator
import os
from typing import Callable

import aiohttp

from services.security_utils import is_safe_url

from .workspace import AgentWorkspace

_TG_MAX_BYTES = 48 * 1024 * 1024


async def _tool_fetch_json(url: str) -> str:
    if not is_safe_url(url):
        return f"fetch_json error: unsafe URL blocked"
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
    """Read the last N lines of the bot's own log file from the host."""
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
    if not os.path.exists(log_path):
        return "bot.log not found."
    try:
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])[-3000:] or "(empty)"
    except Exception as e:
        return f"Error reading bot.log: {e}"


async def _tool_send_workspace_file(rel_path: str, caption: str, ws: "AgentWorkspace", send_cb: Callable) -> str:
    """Read a binary file from workspace and send as document."""
    try:
        full = ws._safe_path(rel_path)
    except ValueError as e:
        return f"Access denied: {e}"
    if not os.path.exists(full):
        return f"[НЕ НАЙДЕНО] File not found: {rel_path}"
    size = os.path.getsize(full)
    if size > _TG_MAX_BYTES:
        return f"File too large ({size // 1024 // 1024} MB > 48 MB)."
    with open(full, "rb") as f:
        data = f.read()
    filename = os.path.basename(rel_path)
    await send_cb({"type": "document", "data": data,
                   "caption": caption[:1024] or filename, "filename": filename})
    return f"[ОТПРАВЛЕНО] Файл '{filename}' ({size // 1024} KB) отправлен."


async def _tool_create_file(filename: str, content: str, caption: str, send_cb: Callable) -> str:
    data = content.encode("utf-8")
    if len(data) > _TG_MAX_BYTES:
        return f"File too large ({len(data) // 1024} KB)."
    await send_cb({"type": "document", "data": data,
                   "caption": caption[:1024] or filename, "filename": filename or "file.txt"})
    return f"File '{filename}' sent."

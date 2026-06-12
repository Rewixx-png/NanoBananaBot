"""
Agentic loop for NanoHatani bot.
ReAct pattern: think → search/scrape → generate_project → reply
"""
import asyncio
import hashlib
import logging
import re
from collections import deque
from typing import Any, Callable, Optional, Tuple

import aiohttp

from ai_services import generate_project_with_gemini
from keys_manager import load_keys, load_firecrawl_keys, remove_key

logger = logging.getLogger(__name__)

MAX_STEPS = 14
_SEARCH_TIMEOUT = 20.0
_SCRAPE_TIMEOUT = 25.0
_LLM_TIMEOUT = 60.0
_PROJECT_TIMEOUT = 180.0


# ── Loop safety ──────────────────────────────────────────────────

class _DebounceHook:
    """Blocks repeated identical tool calls (loop detection)."""

    def __init__(self, window: int = 6, max_repeats: int = 2):
        self._window: deque[str] = deque(maxlen=window)
        self._max = max_repeats

    def check(self, name: str, args: dict) -> Optional[str]:
        fp = hashlib.md5(f"{name}:{sorted(args.items())}".encode()).hexdigest()
        if sum(1 for f in self._window if f == fp) >= self._max:
            return (
                f"LOOP DETECTED: '{name}' called with identical arguments {self._max}+ times. "
                "Change your approach — use different search terms or a different tool."
            )
        self._window.append(fp)
        return None


class _ToolBudget:
    """Hard per-tool call limits per agent run."""

    LIMITS = {"web_search": 6, "scrape_url": 8, "generate_project": 2, "think": 30, "reply": 3}

    def __init__(self):
        self._counts: dict[str, int] = {}

    def charge(self, name: str) -> Optional[str]:
        limit = self.LIMITS.get(name, 20)
        count = self._counts.get(name, 0) + 1
        self._counts[name] = count
        if count > limit:
            return f"BUDGET: '{name}' exceeded limit of {limit} calls. Use a different tool."
        return None


# ── Tool implementations ─────────────────────────────────────────

async def _fc_search(query: str) -> str:
    keys = load_firecrawl_keys()
    if not keys:
        return '{"error": "Firecrawl keys unavailable."}'
    payload = {
        "query": query[:500],
        "limit": 6,
        "sources": [{"type": "web"}],
        "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
    }
    for key in keys:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/search",
                    json=payload, headers=headers,
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
                            url = r.get("url", "")
                            body = (
                                r.get("markdown") or
                                r.get("description") or
                                r.get("metadata", {}).get("description", "")
                            )[:800]
                            if url:
                                parts.append(f"### {title}\nURL: {url}\n{body}".strip())
                        return "\n\n".join(parts) or "No results found."
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
                        continue
        except Exception as e:
            logger.warning(f"agent web_search query={query!r}: {type(e).__name__}: {e}")
    return "Search unavailable — all Firecrawl keys exhausted."


async def _fc_scrape(url: str) -> str:
    keys = load_firecrawl_keys()
    if not keys:
        return "Firecrawl keys unavailable."
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "removeBase64Images": True,
        "maxAge": 3_600_000,
    }
    for key in keys:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/scrape",
                    json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_SCRAPE_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        d = data.get("data") or data
                        text = (d.get("markdown") or d.get("content") or "").strip()
                        return text[:8000] or "Page is empty."
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
                        continue
        except Exception as e:
            logger.warning(f"agent scrape url={url!r}: {type(e).__name__}: {e}")
    return "Could not read the page."


# ── Gemini LLM call ──────────────────────────────────────────────

_SYSTEM = (
    "Ты — Hatani AI, агент-исполнитель. Задачи выполняешь профессионально, без лишних слов.\n\n"
    "ПРОТОКОЛ РАБОТЫ:\n"
    "1. think → обдумай задачу и составь план действий\n"
    "2. web_search → ищи с РАЗНЫХ углов (2-4 запроса по одной теме)\n"
    "3. scrape_url → читай важные страницы целиком\n"
    "4. think → синтезируй найденное, пойми что ещё нужно\n"
    "5. generate_project → когда собрал достаточно, создай проект с ПОЛНЫМ ТЗ\n"
    "   или reply → если проект не нужен\n\n"
    "КРИТИЧНО для generate_project.prompt:\n"
    "- Включи ВСЮ найденную информацию: структуры данных, API, примеры\n"
    "- Опиши дизайн, функции, поведение — чем подробнее, тем лучше результат\n"
    "- Минимум 300 символов в промпте\n\n"
    "ЗАПРЕЩЕНО: вызывать один инструмент с теми же параметрами дважды подряд."
)

_TOOLS = [
    {
        "name": "think",
        "description": (
            "Internal reasoning scratchpad — plan, analyze findings, decide next step. "
            "ALWAYS use before the first search and before generate_project. "
            "Invisible to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {"type": "string", "description": "Your analysis and plan"}
            },
            "required": ["thought"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the internet via Firecrawl. Returns titles, URLs, and page content. "
            "Use multiple different queries to cover the topic from different angles. "
            "Do NOT repeat the same query twice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Precise search query (3-10 words). Be specific.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "scrape_url",
        "description": (
            "Read the full content of a specific web page by URL. "
            "Use after web_search to get complete details from the most relevant pages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL of the page"}
            },
            "required": ["url"],
        },
    },
    {
        "name": "generate_project",
        "description": (
            "Generate a complete project (website, program, bot, app) and deliver it as files. "
            "Include ALL gathered research in the prompt — structure, data, API details, "
            "design requirements, examples. The more detailed the prompt, the better the result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Detailed specification with all research context. "
                        "Minimum 300 characters."
                    ),
                }
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "reply",
        "description": (
            "Send a final text reply to the user and end the task. "
            "Use ONLY when no project/code is needed, or the task is impossible."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Final response text"}
            },
            "required": ["text"],
        },
    },
]


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
    for key in keys:
        # API key in header, not URL — keeps key out of access logs
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key},
                    timeout=aiohttp.ClientTimeout(total=_LLM_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("candidates", [{}])[0]
                    if resp.status in (429, 403):
                        remove_key(key, resp.status)
                        continue
                    body = await resp.text()
                    logger.warning(f"agent gemini {resp.status}: {body[:200]}")
        except Exception as e:
            logger.warning(f"agent gemini call: {type(e).__name__}: {e}")
    return {}


# ── Execute one tool call safely ─────────────────────────────────

async def _execute_tool(
    name: str,
    args: dict,
    debounce: _DebounceHook,
    budget: _ToolBudget,
    status_cb: Callable,
) -> Tuple[str, Optional[dict]]:
    """
    Returns (result_text, project_payload_or_None).
    result_text is always set; project_payload only for generate_project.
    """
    async def _st(text: str):
        try:
            await status_cb(text)
        except Exception:
            pass

    # Loop & budget guards
    if err := debounce.check(name, args):
        return err, None
    if err := budget.charge(name):
        return err, None

    if name == "think":
        thought = args.get("thought", "")
        logger.info(f"[agent:think] {thought[:300]}")
        await _st(f"💭 {thought[:120]}...")
        return "ok", None

    elif name == "web_search":
        query = args.get("query", "")
        await _st(f"🔎 Ищу: «{query[:80]}»")
        result = await _fc_search(query)
        logger.info(f"[agent:web_search] query={query!r} len={len(result)}")
        return result[:6000], None

    elif name == "scrape_url":
        url = args.get("url", "")
        await _st(f"📄 Читаю страницу...")
        result = await _fc_scrape(url)
        logger.info(f"[agent:scrape_url] url={url!r} len={len(result)}")
        return result[:6000], None

    elif name == "generate_project":
        prompt = args.get("prompt", "")
        await _st("⚙️ Генерирую проект по собранным данным...")
        try:
            project = await asyncio.wait_for(
                generate_project_with_gemini(prompt),
                timeout=_PROJECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return "generate_project timed out. Try a shorter prompt.", None
        if project.get("ok"):
            return "__PROJECT_DONE__", project
        err = project.get("error", "unknown error")
        logger.warning(f"[agent:generate_project] failed: {err}")
        return f"Project generation failed: {err}. Try rephrasing the prompt.", None

    elif name == "reply":
        return args.get("text", "Done."), None

    else:
        return f"Unknown tool: {name}", None


# ── Public API ───────────────────────────────────────────────────

async def run_agent(
    task: str,
    chat_id: int,
    username: str,
    status_cb: Callable[[str], Any],
) -> Tuple[Optional[str], Optional[dict]]:
    """
    Runs the agentic loop.
    Returns (text_reply, project_payload) — exactly one is set.
    """
    keys = load_keys()
    if not keys:
        return "Gemini keys are dead.", None

    debounce = _DebounceHook()
    budget = _ToolBudget()
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

        # Inject warning 3 steps before limit so agent can wrap up
        if MAX_STEPS - step <= 3:
            warning = (
                f"\n[СИСТЕМА: осталось {MAX_STEPS - step} шагов. "
                "Вызови generate_project или reply немедленно.]"
            )
            contents.append({"role": "user", "parts": [{"text": warning}]})

        candidate = await _gemini_call(keys, contents)
        if not candidate:
            return "Agent failed — Gemini did not respond.", None

        parts = candidate.get("content", {}).get("parts", [])
        if not parts:
            return "Agent returned empty response.", None

        contents.append({"role": "model", "parts": parts})

        fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if not fn_calls:
            # Pure text response — treat as final reply
            text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
            return text or "Done.", None

        # Execute all function calls (Gemini may call several in parallel)
        tool_responses: list = []

        for fn in fn_calls:
            name = fn.get("name", "")
            args = fn.get("args", {})

            result_text, project = await _execute_tool(
                name, args, debounce, budget, status_cb
            )

            # generate_project succeeded — return immediately
            if name == "generate_project" and project is not None:
                return None, project

            # reply — return immediately
            if name == "reply":
                return result_text, None

            tool_responses.append({
                "functionResponse": {
                    "name": name,
                    "response": {"result": result_text},
                }
            })

        contents.append({"role": "user", "parts": tool_responses})

    return "Agent exhausted all steps without completing the task.", None


def is_agent_task(prompt: str) -> bool:
    """True when the prompt combines web research with creation."""
    lower = prompt.lower()
    web = {
        "найди", "поищи", "загугли", "чекни", "чекнуть", "узнай", "разузнай",
        "в инете", "в интернете", "погугли", "research", "look up", "find info",
        "поищи инфу", "что нового",
    }
    create = {
        "сделай", "создай", "напиши", "сгенерируй", "собери", "склепай", "сверстай",
        "сайт", "страницу", "страничку", "программу", "скрипт", "бота", "приложение",
        "проект", "html", "питон", "python", "визуализацию", "дашборд", "dashboard",
        "инфографику", "парсер", "скрипт",
    }
    return any(w in lower for w in web) and any(c in lower for c in create)

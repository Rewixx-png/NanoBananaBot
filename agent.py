import asyncio
import logging
import aiohttp
from typing import Any, Callable, Optional, Tuple

from keys_manager import load_keys, load_firecrawl_keys, remove_key
from ai_services import generate_project_with_gemini

logger = logging.getLogger(__name__)

MAX_STEPS = 14

_SYSTEM = (
    "Ты — Hatani AI, агент-исполнитель. Говоришь кратко и по делу, задачи выполняешь профессионально.\n\n"
    "У тебя есть инструменты. Используй их последовательно:\n"
    "1. think — обдумай задачу перед каждым шагом\n"
    "2. web_search — ищи инфу (делай несколько запросов с разных углов)\n"
    "3. scrape_url — читай важные страницы полностью\n"
    "4. generate_project — когда собрал достаточно контекста, создавай проект\n"
    "5. reply — только если проект не нужен или задача невозможна\n\n"
    "ВАЖНО: в generate_project.prompt включай ВСЮ найденную информацию — "
    "структуру, данные, контент, примеры. Чем подробнее ТЗ — тем лучше результат."
)

_TOOLS = [
    {
        "name": "think",
        "description": "Внутренние размышления: план, анализ найденного, решение что делать дальше. Невидимо пользователю.",
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {"type": "string", "description": "Твои мысли о задаче и следующем шаге"}
            },
            "required": ["thought"],
        },
    },
    {
        "name": "web_search",
        "description": "Поиск в интернете. Возвращает заголовки, URL, сниппеты и контент страниц.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос (3-10 слов, конкретный)"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "scrape_url",
        "description": "Читает полное содержимое страницы по URL. Используй после web_search для глубокого изучения.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Полный URL страницы"}
            },
            "required": ["url"],
        },
    },
    {
        "name": "generate_project",
        "description": (
            "Генерирует полноценный проект (сайт, программу, бота, приложение) и отправляет пользователю файлами. "
            "В prompt включи: всю найденную информацию, структуру, данные, требования к дизайну и функциональности."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Детальное ТЗ со всем собранным контекстом. Минимум 300 символов.",
                }
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "reply",
        "description": "Финальный текстовый ответ пользователю. Используй только если проект не нужен или задача невозможна.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Текст ответа"}
            },
            "required": ["text"],
        },
    },
]


async def _fc_search(query: str) -> str:
    keys = load_firecrawl_keys()
    if not keys:
        return "Firecrawl ключей нет."
    payload = {"query": query[:500], "limit": 6, "sources": [{"type": "web"}]}
    for key in keys:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/search",
                    json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("data", [])
                        if isinstance(results, dict):
                            results = results.get("results", [])
                        parts = []
                        for r in results[:6]:
                            title = r.get("title") or r.get("metadata", {}).get("title", "")
                            url = r.get("url", "")
                            body = (r.get("markdown") or r.get("description") or r.get("metadata", {}).get("description", ""))[:700]
                            if url:
                                parts.append(f"### {title}\n{url}\n{body}".strip())
                        return "\n\n".join(parts) or "Результатов нет."
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
                        continue
        except Exception as e:
            logger.warning(f"agent web_search: {e}")
    return "Поиск недоступен."


async def _fc_scrape(url: str) -> str:
    keys = load_firecrawl_keys()
    if not keys:
        return "Firecrawl ключей нет."
    payload = {"url": url, "formats": ["markdown"], "onlyMainContent": True, "removeBase64Images": True}
    for key in keys:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/scrape",
                    json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        d = data.get("data") or data
                        text = (d.get("markdown") or d.get("content") or "").strip()
                        return text[:8000] or "Страница пустая."
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
                        continue
        except Exception as e:
            logger.warning(f"agent scrape: {e}")
    return "Не удалось прочитать страницу."


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
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={key}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("candidates", [{}])[0]
                    if resp.status in (429, 403):
                        remove_key(key, resp.status)
                        continue
                    logger.warning(f"agent gemini {resp.status}: {(await resp.text())[:200]}")
        except Exception as e:
            logger.warning(f"agent gemini call: {e}")
    return {}


async def run_agent(
    task: str,
    chat_id: int,
    username: str,
    status_cb: Callable[[str], Any],
) -> Tuple[Optional[str], Optional[dict]]:
    """
    Runs the agentic loop.
    Returns (text_reply, project_payload) — one of them is set, the other None.
    """
    keys = load_keys()
    if not keys:
        return ("Gemini ключи сдохли.", None)

    async def _st(text: str):
        try:
            await status_cb(text)
        except Exception:
            pass

    contents: list = [
        {"role": "user", "parts": [{"text": f"Задача от {username}:\n{task}"}]}
    ]

    for step in range(MAX_STEPS):
        await _st(f"🤖 Агент думает... (шаг {step + 1})")
        candidate = await _gemini_call(keys, contents)

        if not candidate:
            return ("Агент упал — Gemini не ответил.", None)

        parts = candidate.get("content", {}).get("parts", [])
        if not parts:
            return ("Агент вернул пустой ответ.", None)

        # Append model turn to history
        contents.append({"role": "model", "parts": parts})

        fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if not fn_calls:
            # Pure text response — treat as final reply
            text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
            return (text or "Готово.", None)

        tool_responses: list = []

        for fn in fn_calls:
            name = fn.get("name", "")
            args = fn.get("args", {})

            if name == "think":
                thought = args.get("thought", "")
                logger.info(f"[agent:think] {thought[:300]}")
                await _st(f"💭 {thought[:100]}...")
                tool_responses.append({
                    "functionResponse": {"name": "think", "response": {"ok": True}}
                })

            elif name == "web_search":
                query = args.get("query", "")
                await _st(f"🔎 Ищу: «{query[:80]}»")
                result = await _fc_search(query)
                logger.info(f"[agent:web_search] query={query!r} result_len={len(result)}")
                tool_responses.append({
                    "functionResponse": {"name": "web_search", "response": {"result": result[:6000]}}
                })

            elif name == "scrape_url":
                url = args.get("url", "")
                await _st(f"📄 Читаю страницу...")
                result = await _fc_scrape(url)
                logger.info(f"[agent:scrape_url] url={url!r} result_len={len(result)}")
                tool_responses.append({
                    "functionResponse": {"name": "scrape_url", "response": {"result": result[:6000]}}
                })

            elif name == "generate_project":
                prompt = args.get("prompt", "")
                await _st("⚙️ Генерирую проект по собранным данным...")
                project = await generate_project_with_gemini(prompt)
                if project.get("ok"):
                    return (None, project)
                err = project.get("error", "unknown")
                logger.warning(f"[agent:generate_project] failed: {err}")
                tool_responses.append({
                    "functionResponse": {"name": "generate_project", "response": {"error": err}}
                })

            elif name == "reply":
                return (args.get("text", "Готово."), None)

            else:
                tool_responses.append({
                    "functionResponse": {"name": name, "response": {"error": f"unknown tool: {name}"}}
                })

        contents.append({"role": "user", "parts": tool_responses})

    return ("Агент исчерпал все шаги.", None)


def is_agent_task(prompt: str) -> bool:
    """True if the prompt combines web research with creation/generation."""
    lower = prompt.lower()
    web = {'найди', 'поищи', 'загугли', 'чекни', 'чекнуть', 'узнай', 'в инете', 'в интернете',
           'погугли', 'поищи', 'разузнай', 'research', 'look up', 'find info'}
    create = {'сделай', 'создай', 'напиши', 'сгенерируй', 'собери', 'склепай', 'сверстай',
               'сайт', 'страницу', 'программу', 'скрипт', 'бота', 'приложение', 'проект',
               'html', 'питон', 'python', 'визуализацию', 'дашборд', 'dashboard'}
    return any(w in lower for w in web) and any(c in lower for c in create)

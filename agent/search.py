"""Web search, scraping, image URL discovery, and download helpers."""

import asyncio
import logging
import re
from typing import Optional

import aiohttp

from keys import load_firecrawl_keys, remove_key
from services.gemini_image import analyze_photo_with_gemini

from config import AGENT_SCRAPE_TIMEOUT, AGENT_SEARCH_TIMEOUT, TELEGRAM_MEDIA_MAX_BYTES
logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = AGENT_SEARCH_TIMEOUT
_SCRAPE_TIMEOUT = AGENT_SCRAPE_TIMEOUT
_TG_MAX_BYTES = TELEGRAM_MEDIA_MAX_BYTES


async def _fc_search(query: str) -> str:
    """Search via DuckDuckGo (primary, free, no key) then Firecrawl as fallback."""
    # Primary: DuckDuckGo
    try:
        from ddgs import DDGS
        results = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: list(DDGS().text(query[:500], max_results=8))
        )
        if results:
            parts = []
            for r in results:
                title = r.get("title", "")
                url   = r.get("href", "")
                body  = r.get("body", "")[:600]
                if url:
                    parts.append(f"### {title}\nURL: {url}\n{body}".strip())
            if parts:
                return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"DDG search {query!r}: {e}")
    # Fallback: Firecrawl
    last_err = ""

    # Fallback: Firecrawl if keys available
    keys = await load_firecrawl_keys()
    for key in keys:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/search",
                    json={"query": query[:500], "limit": 6,
                          "sources": [{"type": "web"}]},
                    headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=_SEARCH_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("data", [])
                        if isinstance(results, dict):
                            results = results.get("results", []) or results.get("web", [])
                        parts = []
                        for r in results[:6]:
                            title = r.get("title", "")
                            url   = r.get("url", "")
                            body  = (r.get("markdown") or r.get("description") or "")[:600]
                            if url:
                                parts.append(f"### {title}\nURL: {url}\n{body}".strip())
                        return "\n\n".join(parts) or "No results."
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
        except Exception as e:
            logger.warning(f"Firecrawl search {query!r}: {e}")
            last_err = f"{type(e).__name__}: {e}"
    return f"Поиск недоступен. Ошибка: {last_err}" if last_err else "Поиск недоступен."


async def _fc_scrape(url: str) -> str:
    last_err = ""

    # Primary: Jina Reader (free, no key, handles JS-rendered pages)
    jina_url = f"https://r.jina.ai/{url}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                jina_url,
                headers={"Accept": "text/plain", "X-Return-Format": "markdown",
                         "User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=_SCRAPE_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    text = (await resp.text()).strip()
                    if len(text) > 200:
                        return text[:8000]
    except Exception as e:
        logger.warning(f"Jina Reader scrape {url!r}: {e}")
        last_err = f"Jina: {type(e).__name__}"


    # Last resort: Firecrawl if keys available
    keys = await load_firecrawl_keys()
    for key in keys:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.firecrawl.dev/v2/scrape",
                    json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=_SCRAPE_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        d = (await resp.json()).get("data") or {}
                        return (d.get("markdown") or d.get("content") or "")[:8000]
                    if resp.status in (401, 402):
                        remove_key(key, resp.status)
        except Exception as e:
            logger.warning(f"Firecrawl scrape {url!r}: {e}")
            last_err = f"{type(e).__name__}: {e}"

    return f"Не смог прочитать страницу. Ошибка: {last_err}" if last_err else "Не смог прочитать страницу."


async def _search_image_urls(query: str) -> list[str]:
    """Find image URLs via Firecrawl search + page scraping for ogImage metadata."""
    seen: set[str] = set()
    result: list[str] = []
    img_pattern = re.compile(
        r'https?://[^\s\)"\'>]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s\)"\'>]*)?',
        re.IGNORECASE,
    )
    skip_words = ('icon', 'logo', 'favicon', 'avatar', '1x1', 'pixel', 'sprite', 'button')

    def _add(url: str):
        url = url.rstrip('.,;)>"\']')
        if url and url not in seen and not any(s in url.lower() for s in skip_words):
            seen.add(url)
            result.append(url)

    # Step 1: Firecrawl search — extract ogImage from metadata + image URLs from markdown
    search_queries = [
        f"{query} hd wallpaper screenshot",
        f"{query} image",
    ]
    fc_keys = await load_firecrawl_keys()
    if fc_keys:
        for q in search_queries:
            payload = {"query": q[:400], "limit": 6, "sources": [{"type": "web"}],
                       "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True}}
            for key in fc_keys:
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
                                pages = data.get("data", [])
                                if isinstance(pages, dict):
                                    pages = pages.get("results", []) or pages.get("web", [])
                                for page in pages:
                                    og = (page.get("metadata") or {}).get("ogImage", "")
                                    if og:
                                        _add(og)
                                    md = page.get("markdown", "") or page.get("description", "")
                                    for u in img_pattern.findall(md or ""):
                                        _add(u)
                            elif resp.status in (401, 402):
                                remove_key(key, resp.status)
                                continue
                            break
                except Exception as e:
                    logger.warning(f"_search_image_urls search: {e}")
            if len(result) >= 6:
                break

    # Step 2: If still short, scrape top result pages directly for more images
    if len(result) < 4 and fc_keys:
        fc_text = await _fc_search(f"{query} wiki OR fandom OR game")
        page_urls = re.findall(r'https?://[^\s\)"\'<>]+(?:wiki|fandom|igdb|steam)[^\s\)"\'<>]*', fc_text, re.IGNORECASE)
        for page_url in page_urls[:3]:
            page_content = await _fc_scrape(page_url)
            for u in img_pattern.findall(page_content):
                _add(u)
            if len(result) >= 8:
                break

    return result[:12]


async def _download_bytes(url: str, max_bytes: int = _TG_MAX_BYTES) -> Optional[bytes]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                return data if len(data) <= max_bytes else None
    except Exception:
        return None


async def _image_is_relevant(img_bytes: bytes, description: str) -> bool:
    """Ask Gemini if this image matches the description."""
    try:
        answer = await analyze_photo_with_gemini(
            img_bytes,
            f"Does this image show or relate to: {description}? "
            "Reply with only YES or NO.",
        )
        return answer.strip().upper().startswith("YES")
    except Exception:
        return False

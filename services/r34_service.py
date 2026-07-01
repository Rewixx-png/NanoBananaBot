"""R34 / booru art search — uses Firecrawl to find images across booru sites."""
import asyncio
import logging
import random
import aiohttp
from typing import List, Optional
from urllib.parse import quote

from services.security_utils import is_safe_url

logger = logging.getLogger(__name__)

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0',
}

_BOORU_DOMAINS = (
    'rule34.xxx', 'gelbooru.com', 'danbooru.donmai.us',
    'konachan.com', 'yande.re', 'e621.net', 'safebooru.org',
    'xbooru.com', 'realbooru.com', 'rule34.us', 'hypnohub.net',
    'tbib.org', 'sankakucomplex.com', 'zerochan.net',
    'derpibooru.org', 'anime-pictures.net',
)


async def _firecrawl_search(query: str) -> List[str]:
    """Search via Firecrawl API and return image URLs."""
    from keys import load_firecrawl_keys
    keys = await load_firecrawl_keys()
    if not keys:
        return []
    key = keys[0]
    url = 'https://api.firecrawl.dev/v2/search'
    payload = {
        'query': query,
        'limit': 8,
        'sources': [{'type': 'web'}],
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                url, json=payload,
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw = data.get('data', [])
                    if isinstance(raw, dict):
                        raw = raw.get('results', []) or raw.get('web', []) or []
                    urls = [r['url'] for r in raw if isinstance(r, dict) and r.get('url')]
                    return urls
        except Exception as e:
            logger.warning(f'Firecrawl search failed: {e}')
    return []


async def _scrape_page_for_images(session: aiohttp.ClientSession, url: str) -> List[str]:
    """Scrape a booru page for direct image URLs using Firecrawl scrape."""
    from keys import load_firecrawl_keys
    keys = await load_firecrawl_keys()
    if not keys:
        return []
    key = keys[0]
    try:
        async with session.post(
            'https://api.firecrawl.dev/v2/scrape',
            json={'url': url, 'formats': ['markdown']},
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            md = data.get('data', {}).get('markdown', '')
            if isinstance(md, str):
                import re
                imgs = re.findall(r'!\[[^\]]*\]\((https?://[^\)]+\.(?:jpg|jpeg|png|webp|gif)[^\)]*)\)', md, re.IGNORECASE)
                imgs += re.findall(r'(https?://[^\s\)]+\.(?:jpg|jpeg|png|webp|gif))', md, re.IGNORECASE)
                return list(dict.fromkeys(imgs))[:10]
            return []
    except Exception:
        return []


async def search_r34(tag: str, count: int = 4) -> List[tuple]:
    """Search booru sites for tagged images via Firecrawl.

    Returns list of (source_name, image_url) tuples, shuffled.
    """
    count = max(1, min(count, 8))
    # Pick random domains to search
    domains = random.sample(_BOORU_DOMAINS, min(5, len(_BOORU_DOMAINS)))
    queries = [f'site:{d} {tag}' for d in domains]

    # Phase 1: Firecrawl search to find booru page URLs
    all_page_urls = []
    for query in queries:
        urls = await _firecrawl_search(query)
        all_page_urls.extend(urls)

    if not all_page_urls:
        return []

    # Phase 2: Scrape found pages for direct image URLs
    random.shuffle(all_page_urls)
    async with aiohttp.ClientSession() as session:
        tasks = [_scrape_page_for_images(session, url) for url in all_page_urls[:5]]
        results = await asyncio.gather(*tasks)

    all_images = []
    for img_urls in results:
        # Guess source from URL domain
        for url in img_urls:
            src = 'booru'
            for d in _BOORU_DOMAINS:
                if d in url:
                    src = d.split('.')[0].capitalize()
                    break
            all_images.append((src, url))

    random.shuffle(all_images)
    result = []
    seen = set()
    for src, url in all_images:
        if url not in seen:
            seen.add(url)
            result.append((src, url))
        if len(result) >= count:
            break
    return result


async def download_image_bytes(session: aiohttp.ClientSession, url: str,
                               max_size: int = 10 * 1024 * 1024) -> Optional[bytes]:
    """Download an image, returning bytes if under max_size."""
    if not is_safe_url(url):
        return None
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) <= max_size:
                    return data
    except Exception:
        pass
    return None

"""R34 / booru art search — searches DuckDuckGo images targeting booru sites."""
import asyncio
import logging
import random
import re
import aiohttp
from typing import List, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0',
}

# Booru domains to search across
_BOORU_DOMAINS = (
    'rule34.xxx', 'gelbooru.com', 'danbooru.donmai.us',
    'konachan.com', 'yande.re', 'e621.net', 'safebooru.org',
    'xbooru.com', 'realbooru.com', 'rule34.us', 'hypnohub.net',
    'tbib.org', 'sankakucomplex.com', 'zerochan.net',
    'derpibooru.org', 'anime-pictures.net',
)

_IMG_RE = re.compile(r'"image":"(https?://[^"]+\.(?:jpg|jpeg|png|webp|gif))"', re.IGNORECASE)
_URL_RE = re.compile(r'"url":"(https?://[^"]+)"', re.IGNORECASE)
_THUMB_RE = re.compile(r'"thumbnail":"(https?://[^"]+)"', re.IGNORECASE)


async def _search_source(session: aiohttp.ClientSession, tag: str,
                         domain: str) -> List[tuple]:
    """Search DuckDuckGo for images from a specific booru domain."""
    query = f'site:{domain} {tag}'
    url = f'https://lite.duckduckgo.com/lite/?q={quote(query)}'
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
            # Extract external URLs from DDG lite results
            hrefs = re.findall(r'class="result-link"[^>]*href="([^"]+)"', text)
            # Filter to image URLs
            image_urls = [h for h in hrefs if re.search(r'\.(?:jpg|jpeg|png|webp|gif)(?:\?|$)', h, re.IGNORECASE)
                          or any(d in h for d in ('/images/', '/samples/', '/img/', '/data/', 'files.'))]
            # If no direct image URLs, try the booru site pages
            if not image_urls:
                booru_urls = [h for h in hrefs if any(d in h for d in _BOORU_DOMAINS)]
                if booru_urls:
                    for booru_url in booru_urls[:3]:
                        try:
                            async with session.get(booru_url, headers=_HEADERS,
                                                   timeout=aiohttp.ClientTimeout(total=8)) as bresp:
                                if bresp.status == 200:
                                    btext = await bresp.text()
                                    imgs = re.findall(r'(?:file_url|data-file-url)="(https?://[^"]+\.(?:jpg|jpeg|png|webp))"', btext)
                                    imgs += re.findall(r'"file_url":"(https?://[^"]+\.(?:jpg|jpeg|png|webp))"', btext)
                                    image_urls.extend(imgs)
                        except Exception:
                            continue
            return [(domain.split('.')[0].capitalize(), u) for u in list(dict.fromkeys(image_urls))[:5]]
    except Exception:
        return []


async def search_r34(tag: str, count: int = 4) -> List[tuple]:
    """Search booru sites for tagged images via DuckDuckGo.

    Returns list of (source_name, image_url) tuples, shuffled.
    """
    count = max(1, min(count, 8))
    domains = random.sample(_BOORU_DOMAINS, min(10, len(_BOORU_DOMAINS)))

    async with aiohttp.ClientSession() as session:
        tasks = [_search_source(session, tag, domain) for domain in domains]
        results = await asyncio.gather(*tasks)

    all_images = []
    for img_list in results:
        all_images.extend(img_list)

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
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) <= max_size:
                    return data
    except Exception:
        pass
    return None

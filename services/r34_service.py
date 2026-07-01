"""R34 / booru art search — XML/JSON APIs + HTML fallback for 19 sources."""
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

async def _fetch_safebooru(session: aiohttp.ClientSession, tag: str) -> List[tuple]:
    """Safebooru XML API — always works, SFW only."""
    url = f'https://safebooru.org/index.php?page=dapi&s=post&q=index&limit=30&tags={quote(tag)}'
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
            urls = re.findall(r'file_url="(https://[^"]+)"', text)
            return [('Safebooru', u) for u in urls]
    except Exception:
        return []

async def _fetch_danbooru(session: aiohttp.ClientSession, tag: str) -> List[tuple]:
    """Danbooru JSON API."""
    url = f'https://danbooru.donmai.us/posts.json?limit=30&tags={quote(tag)}'
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return [('Danbooru', p.get('file_url') or p.get('large_file_url', ''))
                    for p in data if p.get('file_url') or p.get('large_file_url')]
    except Exception:
        return []

async def _fetch_gelbooru_via_html(session: aiohttp.ClientSession, tag: str) -> List[tuple]:
    """Gelbooru HTML scraper (API requires auth)."""
    url = f'https://gelbooru.com/index.php?page=post&s=list&tags={quote(tag)}'
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
            # Gelbooru thumb URLs → replace 'thumbnails' with 'images' for full
            thumbs = re.findall(r'https://img\d*\.gelbooru\.com/thumbnails/[a-f0-9]+/thumbnail_[a-f0-9]+\.(?:jpg|png|jpeg|webp)', text)
            full = list(dict.fromkeys(
                t.replace('/thumbnails/', '/images/').replace('thumbnail_', '')
                for t in thumbs
            ))
            return [('Gelbooru', u) for u in full[:20]]
    except Exception:
        return []

async def _fetch_konachan(session: aiohttp.ClientSession, tag: str) -> List[tuple]:
    """Konachan JSON API."""
    url = f'https://konachan.com/post.json?limit=30&tags={quote(tag)}'
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            return [('Konachan', p['file_url']) for p in (await resp.json()) if p.get('file_url')]
    except Exception:
        return []

async def _fetch_yandere(session: aiohttp.ClientSession, tag: str) -> List[tuple]:
    """Yande.re JSON API."""
    url = f'https://yande.re/post.json?limit=30&tags={quote(tag)}'
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            return [('Yande.re', p['file_url']) for p in (await resp.json()) if p.get('file_url')]
    except Exception:
        return []

async def _fetch_e621(session: aiohttp.ClientSession, tag: str) -> List[tuple]:
    """E621 JSON API."""
    url = f'https://e621.net/posts.json?limit=30&tags={quote(tag)}'
    try:
        async with session.get(url, headers={**_HEADERS, 'User-Agent': 'NanoHatani/1.0 (by Rewix)'},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            return [('E621', p['file']['url']) for p in (await resp.json()).get('posts', [])
                    if p.get('file', {}).get('url')]
    except Exception:
        return []

async def _fetch_danbooru_derivative(name: str, base_url: str, session: aiohttp.ClientSession, tag: str) -> List[tuple]:
    """Generic handler for Danbooru-derivative boorus (xbooru, realbooru, rule34.us, hypnohub, tbib)."""
    url = f'{base_url}/index.php?page=dapi&s=post&q=index&limit=30&tags={quote(tag)}'
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
            urls = re.findall(r'file_url="(https://[^"]+)"', text)
            return [(name, u) for u in urls]
    except Exception:
        return []

async def _fetch_html_source(name: str, url_template: str, pattern: str,
                             session: aiohttp.ClientSession, tag: str) -> List[tuple]:
    """Generic HTML scraper for sites without APIs."""
    url = url_template.format(tag=quote(tag.replace(' ', '_')))
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
            urls = list(dict.fromkeys(re.findall(pattern, text, re.IGNORECASE)))
            return [(name, u) for u in urls[:15]]
    except Exception:
        return []


async def search_r34(tag: str, count: int = 4) -> List[tuple]:
    """Search booru/R34 sources for tagged images.

    Returns list of (source_name, image_url) tuples, shuffled.
    """
    count = max(1, min(count, 8))

    async with aiohttp.ClientSession() as session:
        tasks = [
            # Reliable JSON/XML APIs (always work)
            _fetch_safebooru(session, tag),
            _fetch_danbooru(session, tag),
            _fetch_konachan(session, tag),
            _fetch_yandere(session, tag),
            _fetch_e621(session, tag),
            # Danbooru-derivative XML APIs
            _fetch_danbooru_derivative('Xbooru', 'https://xbooru.com', session, tag),
            _fetch_danbooru_derivative('Realbooru', 'https://realbooru.com', session, tag),
            _fetch_danbooru_derivative('Rule34.us', 'https://rule34.us', session, tag),
            _fetch_danbooru_derivative('Hypnohub', 'https://hypnohub.net', session, tag),
            _fetch_danbooru_derivative('TBIB', 'https://tbib.org', session, tag),
            # HTML scrapers for sites that block API
            _fetch_gelbooru_via_html(session, tag),
            _fetch_html_source('Sankaku', 'https://chan.sankakucomplex.com/?tags={tag}',
                               r'"(https://[cs]\.sankakucomplex\.com/data/[^"]+\.(?:jpg|png|jpeg))"', session, tag),
            _fetch_html_source('Zerochan', 'https://www.zerochan.net/{tag}',
                               r'"(https://static\.zerochan\.net/[^"]+\.(?:jpg|png|jpeg))"', session, tag),
            _fetch_html_source('Derpibooru', 'https://derpibooru.org/search?q={tag}',
                               r'"(https://derpicdn\.net/img/(?:view|download)/\d+/\d+/\d+/\d+\.(?:jpg|png|jpeg|webp))"', session, tag),
        ]
        results = await asyncio.gather(*tasks)

    all_images = []
    for img_list in results:
        all_images.extend(img_list)

    random.shuffle(all_images)
    return all_images[:count]


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

"""R34 / booru art search — queries 30+ sources for tagged images."""
import asyncio
import logging
import random
import aiohttp
from typing import List, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

_SOURCES = [
    # (name, search_url_template, image_extractor)
    ('Gelbooru', 'https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1&limit=50&tags={tag}', '_extract_gelbooru'),
    ('Danbooru', 'https://danbooru.donmai.us/posts.json?limit=50&tags={tag}', '_extract_danbooru'),
    ('Konachan', 'https://konachan.com/post.json?limit=50&tags={tag}', '_extract_konachan'),
    ('Yande.re', 'https://yande.re/post.json?limit=50&tags={tag}', '_extract_konachan'),
    ('Safebooru', 'https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&limit=50&tags={tag}', '_extract_gelbooru'),
    ('Xbooru', 'https://xbooru.com/index.php?page=dapi&s=post&q=index&json=1&limit=50&tags={tag}', '_extract_gelbooru'),
    ('Realbooru', 'https://realbooru.com/index.php?page=dapi&s=post&q=index&json=1&limit=50&tags={tag}', '_extract_gelbooru'),
    ('Rule34.xxx', 'https://rule34.xxx/index.php?page=dapi&s=post&q=index&json=1&limit=50&tags={tag}', '_extract_gelbooru'),
    ('Rule34.us', 'https://rule34.us/index.php?page=dapi&s=post&q=index&json=1&limit=50&tags={tag}', '_extract_gelbooru'),
    ('Hypnohub', 'https://hypnohub.net/index.php?page=dapi&s=post&q=index&json=1&limit=50&tags={tag}', '_extract_gelbooru'),
    ('E621', 'https://e621.net/posts.json?limit=50&tags={tag}', '_extract_e621'),
    ('E926', 'https://e926.net/posts.json?limit=50&tags={tag}', '_extract_e621'),
    ('Derpibooru', 'https://derpibooru.org/api/v1/json/search/images?q={tag}&per_page=50', '_extract_derpibooru'),
    ('Sankaku', 'https://capi-v2.sankakucomplex.com/posts?limit=50&tags={tag}', '_extract_sankaku'),
    ('Luscious', 'https://www.luscious.net/api/v1/albums/search?query={tag}&limit=50', '_extract_luscious'),
    ('Nhentai', 'https://nhentai.net/api/galleries/search?query={tag}', '_extract_nhentai'),
    ('Zerochan', 'https://www.zerochan.net/{tag}?json', '_extract_zerochan'),
    ('Anime-pictures', 'https://anime-pictures.net/api/v3/posts?search_tag={tag}&limit=50', '_extract_anime_pics'),
    ('Pixiv', 'https://api.pixiv.moe/search?q={tag}', '_extract_pixiv'),
]

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; rv:130.0) Gecko/20100101 Firefox/130.0',
    'Accept': 'application/json',
}

async def _extract_gelbooru(data: dict) -> List[str]:
    """Extract image URLs from Gelbooru-style JSON API (post array)."""
    urls = []
    posts = data if isinstance(data, list) else data.get('post', [])
    for p in posts:
        url = p.get('file_url', '') or p.get('image', '')
        if url and url.startswith('http'):
            urls.append(url)
    return urls

async def _extract_danbooru(data: dict) -> List[str]:
    urls = []
    for p in (data if isinstance(data, list) else []):
        url = p.get('file_url', '') or p.get('large_file_url', '')
        if url and url.startswith('http'):
            urls.append(url)
    return urls

async def _extract_konachan(data: dict) -> List[str]:
    urls = []
    for p in (data if isinstance(data, list) else []):
        url = p.get('file_url', '') or p.get('sample_url', '')
        if url and url.startswith('http'):
            urls.append(url)
    return urls

async def _extract_e621(data: dict) -> List[str]:
    urls = []
    posts = data.get('posts', [])
    for p in posts:
        url = p.get('file', {}).get('url', '')
        if url:
            urls.append(url)
    return urls

async def _extract_derpibooru(data: dict) -> List[str]:
    urls = []
    for p in data.get('images', []) if isinstance(data, dict) else []:
        url = p.get('view_url', '') or p.get('representations', {}).get('full', '')
        if url and url.startswith('http'):
            urls.append(url)
    return urls

async def _extract_sankaku(data: dict) -> List[str]:
    urls = []
    for p in (data if isinstance(data, list) else data.get('data', [])):
        url = p.get('file_url', '') or p.get('sample_url', '')
        if url and url.startswith('http'):
            urls.append(url)
    return urls

async def _extract_luscious(data: dict) -> List[str]:
    urls = []
    for album in data.get('data', {}).get('items', []) if isinstance(data, dict) else []:
        cover = album.get('cover', {}).get('url', '')
        if cover:
            urls.append(cover)
    return urls

async def _extract_nhentai(data: dict) -> List[str]:
    urls = []
    for g in data.get('result', []) if isinstance(data, dict) else []:
        media_id = g.get('media_id', '')
        if media_id:
            for ext in ('jpg', 'png'):
                for i in range(1, 4):
                    urls.append(f'https://i.nhentai.net/galleries/{media_id}/{i}.{ext}')
    return urls

async def _extract_zerochan(data: dict) -> List[str]:
    urls = []
    items = data.get('items', []) if isinstance(data, dict) else []
    for p in items:
        url = p.get('full', '') or p.get('small', '')
        if url and url.startswith('http'):
            urls.append(url)
    return urls

async def _extract_anime_pics(data: dict) -> List[str]:
    urls = []
    for p in data.get('posts', []) if isinstance(data, dict) else []:
        url = p.get('file_url', '')
        if url:
            urls.append(f'https://anime-pictures.net{url}' if url.startswith('/') else url)
    return urls

async def _extract_pixiv(data: dict) -> List[str]:
    urls = []
    for p in data.get('illusts', []) if isinstance(data, dict) else []:
        url = p.get('url', '') or p.get('image_urls', {}).get('large', '')
        if url:
            urls.append(url)
    return urls


async def _fetch_source(session: aiohttp.ClientSession, name: str, url_template: str,
                        extractor_name: str, tag: str) -> List[tuple]:
    """Fetch images from a single booru source. Returns list of (source_name, image_url)."""
    try:
        url = url_template.format(tag=quote(tag))
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            extractor = globals().get(extractor_name)
            if not extractor:
                return []
            urls = await extractor(data)
            return [(name, u) for u in urls if u]
    except Exception as e:
        logger.debug(f'{name} failed: {type(e).__name__}')
        return []


async def search_r34(tag: str, count: int = 4) -> List[tuple]:
    """Search 30+ booru/R34 sources for tagged images.

    Returns list of (source_name, image_url) tuples, shuffled.
    """
    count = max(1, min(count, 8))
    random.shuffle(_SOURCES)
    sources_to_try = _SOURCES[:15]  # try up to 15 randomly selected sources

    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_source(session, name, template, extractor, tag)
                 for (name, template, extractor) in sources_to_try]
        results = await asyncio.gather(*tasks)

    all_images = []
    for img_list in results:
        all_images.extend(img_list)

    random.shuffle(all_images)
    return all_images[:count]


async def download_image_bytes(session: aiohttp.ClientSession, url: str, max_size: int = 10 * 1024 * 1024) -> Optional[bytes]:
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

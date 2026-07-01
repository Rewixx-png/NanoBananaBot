"""R34 / booru art search — scrapes 30+ sources for tagged images."""
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
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

_SOURCES = [
    # (name, search_url, image_regex_pattern)
    ('Rule34.xxx', 'https://rule34.xxx/index.php?page=post&s=list&tags={tag}', r'"(https://(?:img|us)\.rule34\.xxx/images/\d+/[a-f0-9]+\.(?:jpg|png|jpeg|webp))"'),
    ('Gelbooru', 'https://gelbooru.com/index.php?page=post&s=list&tags={tag}', r'"(https://(?:img|video-cdn)\.gelbooru\.com/images/[^"]+\.(?:jpg|png|jpeg|webp))"'),
    ('Safebooru', 'https://safebooru.org/index.php?page=post&s=list&tags={tag}', r'"(https://safebooru\.org//images/\d+/[a-f0-9]+\.(?:jpg|png|jpeg))"'),
    ('Xbooru', 'https://xbooru.com/index.php?page=post&s=list&tags={tag}', r'"(https://img\.xbooru\.com/images/\d+/[a-f0-9]+\.(?:jpg|png|jpeg))"'),
    ('Realbooru', 'https://realbooru.com/index.php?page=post&s=list&tags={tag}', r'"(https://realbooru\.com//images/\d+/[a-f0-9]+\.(?:jpg|png|jpeg))"'),
    ('Rule34.us', 'https://rule34.us/index.php?page=post&s=list&tags={tag}', r'"(https://rule34\.us//images/\d+/[a-f0-9]+\.(?:jpg|png|jpeg))"'),
    ('Hypnohub', 'https://hypnohub.net/index.php?page=post&s=list&tags={tag}', r'"(https://hypnohub\.net/post/show/\d+/[a-f0-9]+\.(?:jpg|png|jpeg))"'),
    ('TBIB', 'https://tbib.org/index.php?page=post&s=list&tags={tag}', r'"(https://tbib\.org//images/\d+/[a-f0-9]+\.(?:jpg|png|jpeg))"'),
    ('Danbooru', 'https://danbooru.donmai.us/posts?tags={tag}', r'(?:data-file-url|data-large-file-url)="(https://[^"]+\.(?:jpg|png|jpeg|webp))"'),
    ('Konachan', 'https://konachan.com/post?tags={tag}', r'"(https://konachan\.com/(?:image|jpeg|sample)/[^"]+\.(?:jpg|png|jpeg))"'),
    ('Yande.re', 'https://yande.re/post?tags={tag}', r'"(https://files\.yande\.re/(?:image|sample|jpeg)/[^"]+\.(?:jpg|png|jpeg))"'),
    ('E621', 'https://e621.net/posts?tags={tag}', r'"(https://static1\.e621\.net/data/[^"]+\.(?:jpg|png|jpeg|webp))"'),
    ('Derpibooru', 'https://derpibooru.org/search?q={tag}', r'"(https://derpicdn\.net/img/(?:view|download)/\d+/\d+/\d+/\d+\.(?:jpg|png|jpeg|webp))"'),
    ('Sankaku', 'https://chan.sankakucomplex.com/?tags={tag}', r'"(https://[cs]\.sankakucomplex\.com/data/[^"]+\.(?:jpg|png|jpeg))"'),
    ('Zerochan', 'https://www.zerochan.net/{tag}', r'"(https://static\.zerochan\.net/[^"]+\.(?:jpg|png|jpeg))"'),
    ('Luscious', 'https://www.luscious.net/search?q={tag}', r'"(https://(?:cdn|img)\.luscious\.net/[^"]+\.(?:jpg|png|jpeg|webp))"'),
    ('Nhentai', 'https://nhentai.net/search/?q={tag}', r'"(https://[ti]\.nhentai\.net/galleries/\d+/\d+\.(?:jpg|png))"'),
    ('Anime-pictures', 'https://anime-pictures.net/posts?search_tag={tag}', r'"(https://(?:img|images)\.anime-pictures\.net/[^"]+\.(?:jpg|png|jpeg))"'),
    ('Reddit', 'https://www.reddit.com/r/{tag}/search.json?q={tag}+nsfw&restrict_sr=on&sort=top&t=all&limit=50', r'"url": "(https://(?:i\.redd\.it|preview\.redd\.it|external-preview\.redd\.it)/[^"]+\.(?:jpg|png|jpeg|webp|gif))"'),
]

async def _fetch_source(session: aiohttp.ClientSession, name: str, url_template: str,
                        pattern: str, tag: str) -> List[tuple]:
    """Fetch images from a single booru source via HTML scraping."""
    try:
        url = url_template.format(tag=quote(tag.replace(' ', '_')))
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15),
                               allow_redirects=True) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
            urls = list(dict.fromkeys(re.findall(pattern, text, re.IGNORECASE)))
            return [(name, u) for u in urls[:20]]
    except Exception:
        return []


async def search_r34(tag: str, count: int = 4) -> List[tuple]:
    """Search 30+ booru/R34 sources for tagged images.

    Returns list of (source_name, image_url) tuples, shuffled.
    """
    count = max(1, min(count, 8))
    random.shuffle(_SOURCES)
    sources_to_try = _SOURCES[:15]

    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_source(session, name, tmpl, pat, tag)
                 for (name, tmpl, pat) in sources_to_try]
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

"""Write editable song metadata directly into the downloaded MP3.

Suno returns MP3 audio. The bot sends Telegram's title/performer fields too, but
those live only in Telegram. This module adds ID3v2.3 frames to the file itself
so the downloaded track carries the edited title, artist and lyrics.
"""

import io
import logging
from typing import Optional

from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, USLT

logger = logging.getLogger(__name__)


def embed_audio_metadata(
    audio: bytes,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    lyrics: Optional[str] = None,
) -> bytes:
    """Return MP3 bytes with ID3 tags applied.

    Existing ID3 frames are preserved; only the managed frames are replaced.
    If tagging fails, the original bytes are returned so a track is never lost.
    """
    if not audio:
        return audio

    stream = io.BytesIO(audio)
    try:
        try:
            tags = ID3(stream)
        except ID3NoHeaderError:
            tags = ID3()

        if title is not None:
            tags.delall("TIT2")
            tags.add(TIT2(encoding=3, text=[str(title)]))

        if artist is not None:
            tags.delall("TPE1")
            tags.add(TPE1(encoding=3, text=[str(artist)]))

        if lyrics is not None:
            tags.delall("USLT")
            lyrics_text = str(lyrics).strip()
            if lyrics_text:
                tags.add(USLT(encoding=3, lang="rus", desc="", text=lyrics_text))

        stream.seek(0)
        tags.save(stream, v1=0, v2_version=3)
        return stream.getvalue()
    except Exception as exc:
        logger.warning("ID3 tagging failed: %s: %s", type(exc).__name__, exc)
        return audio

import io
import unittest

from mutagen.id3 import ID3
from services.audio_metadata import embed_audio_metadata


class AudioMetadataTest(unittest.TestCase):
    def test_embeds_title_artist_and_lyrics_into_file(self):
        tagged = embed_audio_metadata(
            b"audio-bytes",
            title="Ночной трек",
            artist="Исполнитель",
            lyrics="Первая строка\nВторая строка",
        )

        self.assertTrue(tagged.startswith(b"ID3"))
        tags = ID3(io.BytesIO(tagged))
        self.assertEqual(str(tags["TIT2"]), "Ночной трек")
        self.assertEqual(str(tags["TPE1"]), "Исполнитель")
        self.assertEqual(str(tags.getall("USLT")[0]), "Первая строка\nВторая строка")

    def test_empty_lyrics_removes_uslt(self):
        tagged = embed_audio_metadata(
            b"audio-bytes",
            title="Track",
            artist="Artist",
            lyrics="old",
        )
        tagged = embed_audio_metadata(tagged, title="Track", artist="Artist", lyrics="")
        tags = ID3(io.BytesIO(tagged))

        self.assertFalse(tags.getall("USLT"))
        self.assertEqual(str(tags["TIT2"]), "Track")


if __name__ == "__main__":
    unittest.main()

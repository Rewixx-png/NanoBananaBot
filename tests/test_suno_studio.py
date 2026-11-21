"""Tests for full SunoAPI.org coverage (all 38 endpoints), DB persistence, and Studio UI."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite


class SunoServiceFullTest(unittest.TestCase):
    def test_build_payload_non_custom_multimodal_drops_custom_only_fields(self):
        from services.suno_service import build_payload

        cfg = {
            'custom_mode': '0',
            'instrumental': '0',
            'style': 'synthwave',
            'lyrics': '[Verse] night',
            'title': 'Forbidden Title',
            'neg': 'metal',
            'vocal': 'm',
            'duration': 120,
            'style_weight': 0.8,
            'weirdness': 0.5,
            'audio_weight': 0.5,
            'variety': 3,
            'image_urls': ['https://x/1.jpg'],
            'video_urls': ['https://x/v.mp4'],
            'audio_urls': ['https://x/a.mp3'],
            'midi_urls': ['https://x/m.mid'],
            'persona_id': 'v_123',
            'persona_model': 'voice_persona',
        }
        p = build_payload('drive at night', 'V6', cfg)
        self.assertFalse(p['customMode'])
        self.assertEqual(p['prompt'], 'drive at night')
        self.assertEqual(p['imageUrls'], ['https://x/1.jpg'])
        self.assertEqual(p['videoUrls'], ['https://x/v.mp4'])
        self.assertEqual(p['audioUrls'], ['https://x/a.mp3'])
        self.assertEqual(p['midiUrls'], ['https://x/m.mid'])
        self.assertEqual(p['personaId'], 'v_123')
        self.assertEqual(p['personaModel'], 'voice_persona')
        for forbidden in ('title', 'negativeTags', 'duration', 'vocalGender', 'styleWeight', 'weirdnessConstraint', 'audioWeight', 'variety'):
            self.assertNotIn(forbidden, p)

    def test_speech_modes_are_mutually_exclusive(self):
        from services.suno_service import generate_speech

        captured = []

        async def fake_submit(path, payload, **kw):
            captured.append((path, payload))
            return [], None

        with patch('services.suno_service._submit_and_await_music', side_effect=fake_submit):
            asyncio.run(generate_speech(custom_mode=False, prompt='radio intro', script='ignored', tone='warm'))
            asyncio.run(generate_speech(custom_mode=True, prompt='ignored', script='Hello world', tone='calm', variety=2, vocal_gender='f', background_music=True))

        self.assertEqual(captured[0][0], '/generate/speech')
        self.assertEqual(captured[0][1]['customMode'], False)
        self.assertEqual(captured[0][1]['prompt'], 'radio intro')
        for k in ('script', 'tone', 'variety', 'vocalGender', 'backgroundMusic'):
            self.assertNotIn(k, captured[0][1])

        self.assertEqual(captured[1][1]['customMode'], True)
        self.assertEqual(captured[1][1]['script'], 'Hello world')
        self.assertEqual(captured[1][1]['tone'], 'calm')
        self.assertEqual(captured[1][1]['variety'], 2)
        self.assertEqual(captured[1][1]['vocalGender'], 'f')
        self.assertTrue(captured[1][1]['backgroundMusic'])
        self.assertNotIn('prompt', captured[1][1])

    def test_replace_section_enforces_min_10s_and_two_modes(self):
        from services.suno_service import replace_section_suno

        _, err = asyncio.run(replace_section_suno(
            infill_start_s=10, infill_end_s=15, prompt='x', full_lyrics='x',
            tags='pop', title='T', task_id='t1', audio_id='a1',
        ))
        self.assertIn('10', err or '')

        captured = []

        async def fake_submit(path, payload, **kw):
            captured.append((path, payload))
            return [], None

        with patch('services.suno_service._submit_and_await_music', side_effect=fake_submit):
            asyncio.run(replace_section_suno(
                infill_start_s=10, infill_end_s=25, prompt='new', full_lyrics='full',
                tags='rock', title='T', task_id='t1', audio_id='a1',
            ))
            asyncio.run(replace_section_suno(
                infill_start_s=10, infill_end_s=25, prompt='new', full_lyrics='full',
                tags='rock', title='T', upload_url='https://x/a.mp3', model_key='V6_WILD',
            ))

        self.assertNotIn('model', captured[0][1])
        self.assertNotIn('uploadUrl', captured[0][1])
        self.assertEqual(captured[0][1]['taskId'], 't1')
        self.assertEqual(captured[1][1]['uploadUrl'], 'https://x/a.mp3')
        self.assertEqual(captured[1][1]['model'], 'V6_WILD')

    def test_voice_api_field_quirks(self):
        from services.suno_service import voice_check_availability, voice_regenerate

        calls = []

        async def fake_call(method, path, **kw):
            calls.append((method, path, kw.get('payload')))
            if path == '/voice/check-voice':
                return {'code': 200, 'data': {'isAvailable': True}}, 'k', None
            return {'code': 200, 'data': {'taskId': 'vt1'}}, 'k', None

        with patch('services.suno_extra._call_with_keys', side_effect=fake_call), \
             patch('services.suno_extra._poll_voice_validate', new=AsyncMock(return_value=({'taskId': 'vt1', 'validateInfo': 'Sing this'}, None))):
            avail, err = asyncio.run(voice_check_availability('vt1'))
            self.assertIsNone(err)
            self.assertTrue(avail)
            data, err2 = asyncio.run(voice_regenerate('vt1'))
            self.assertIsNone(err2)
            self.assertEqual(data['validateInfo'], 'Sing this')

        # check-voice uses snake_case task_id; regenerate uses calBackUrl (single l)
        self.assertEqual(calls[0][2], {'task_id': 'vt1'})
        self.assertIn('calBackUrl', calls[1][2])
        self.assertNotIn('callBackUrl', calls[1][2])

    def test_stem_names_count_and_midi_smf_and_lrc(self):
        from services.suno_service import SUNO_STEM_NAMES, build_midi_bytes, format_lrc

        self.assertEqual(len(SUNO_STEM_NAMES), 98)
        self.assertIn('Lead Vocal', SUNO_STEM_NAMES)
        self.assertIn('Didgeridoo', SUNO_STEM_NAMES)

        midi_bytes = build_midi_bytes({
            'state': 'complete',
            'instruments': [
                {'name': 'Piano', 'notes': [{'pitch': 60, 'start': '0.1', 'end': 0.5, 'velocity': 0.9}]},
                {'name': 'Drums', 'notes': [{'pitch': 36, 'start': 0.0, 'end': 0.2, 'velocity': 1}]},
            ],
        })
        self.assertTrue(midi_bytes.startswith(b'MThd'))
        self.assertIn(b'MTrk', midi_bytes)

        lrc = format_lrc([
            {'word': 'Hello ', 'startS': 1.25},
            {'word': 'world\nSecond ', 'startS': 2.0},
            {'word': 'line', 'startS': 5.5},
        ])
        self.assertIn('[00:01.25] Hello world', lrc)
        self.assertIn('[00:02.00] Second line', lrc)

    def test_suno_db_migration_and_crud(self):
        from database.migrations import apply_migrations
        import database.connection as db_conn
        from database.suno import (
            delete_suno_persona,
            delete_suno_voice,
            get_suno_personas,
            get_suno_stems_by_id,
            get_suno_stems_list,
            get_suno_track_by_id,
            get_suno_tracks,
            get_suno_voices,
            save_suno_persona,
            save_suno_stems,
            save_suno_track,
            save_suno_voice,
        )

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'test_suno.db')
                with patch.object(db_conn, 'DB_PATH', path):
                    async with aiosqlite.connect(path) as db:
                        await apply_migrations(db)

                    tid = await save_suno_track(1, 10, 'task1', 'aud1', title='Night', tags='synth', duration=120)
                    self.assertIsNotNone(tid)
                    tracks = await get_suno_tracks(1)
                    self.assertEqual(len(tracks), 1)
                    t = await get_suno_track_by_id(tid, 1)
                    self.assertEqual(t['audio_id'], 'aud1')

                    pid = await save_suno_persona(1, 'pers_1', 'Cyber', 'Dark synth')
                    self.assertEqual(len(await get_suno_personas(1)), 1)
                    self.assertTrue(await delete_suno_persona(pid, 1))

                    vid = await save_suno_voice(1, 'vt1', 'voice_1', 'MyVoice', skill_level='pro', language='ru')
                    self.assertEqual(len(await get_suno_voices(1)), 1)
                    self.assertTrue(await delete_suno_voice(vid, 1))

                    sid = await save_suno_stems(1, 'stask1', 'Night', 'separate_vocal', [{'id': 's1', 'name': 'Vocals'}])
                    self.assertEqual(len(await get_suno_stems_list(1)), 1)
                    rec = await get_suno_stems_by_id(sid, 1)
                    self.assertEqual(rec['stems'][0]['id'], 's1')

        asyncio.run(run())

    def test_studio_keyboards_callback_data_within_telegram_64_bytes(self):
        from handlers.music_studio_ui import (
            my_tracks_screen,
            studio_action_keyboard,
            studio_audio_keyboard,
            studio_hub_keyboard,
            studio_sounds_keyboard,
            studio_speech_keyboard,
            studio_stem_mode_keyboard,
            studio_stem_picker_keyboard,
            studio_stems_hub_keyboard,
            studio_stems_result_keyboard,
            studio_utils_keyboard,
            track_actions_keyboard,
            voices_hub_keyboard,
        )

        keyboards = [
            studio_hub_keyboard('1234567890ab'),
            studio_audio_keyboard(),
            studio_action_keyboard('abcdefghij', {'kind': 'replace', 'model': 'V6'}),
            studio_action_keyboard('abcdefghij', {'kind': 'mashup', 'model': 'V6_WILD', 'urls': []}),
            studio_sounds_keyboard('abcdefghij', {'model': 'V6', 'sound_key': 'C#m'}),
            studio_speech_keyboard('abcdefghij', {'custom_mode': True, 'variety': 2}),
            studio_stems_hub_keyboard([{'id': 1, 'task_id': 't1', 'source_title': 'Song'}]),
            studio_stem_mode_keyboard('abcdefghij'),
            studio_stem_picker_keyboard('abcdefghij', 0)[1],
            studio_stems_result_keyboard(1, [{'id': 's1', 'name': 'Lead Vocal'}]),
            track_actions_keyboard(999),
            my_tracks_screen([{'id': 1, 'title': 'Song', 'duration': 90}], 0)[1],
            voices_hub_keyboard([{'id': 1, 'name': 'P'}], [{'id': 2, 'name': 'V'}]),
            studio_utils_keyboard(),
        ]
        for kb in keyboards:
            for row in kb.inline_keyboard:
                for btn in row:
                    if btn.callback_data:
                        self.assertLessEqual(len(btn.callback_data.encode('utf-8')), 64, btn.callback_data)


if __name__ == '__main__':
    unittest.main()

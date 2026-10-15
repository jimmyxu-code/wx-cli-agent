import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wechat_bridge as bridge
from backends.receive import voice_runtime as runtime, voice_transcripts as voice


def message(**changes):
    return {'content': 'Voice', 'msg_type': 34, 'talker': 'fixture@chatroom', 'server_id': 9000000000000000123,
            'sort_seq': 1700000000000, 'create_time': 1700000000, 'snippet': '[voice]', **changes}


class VoiceTextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = Mock(return_value={'path': str(self.root / 'audio.mp3'), 'sha256': 'a' * 64})
        self.result = {'status': 'success', 'text': '明天三点开会', 'source': 'local_sensevoice',
                       'model': runtime.VERSION, 'durationMs': 1200, 'audioUploaded': False}

    def enrich(self, items, **changes):
        return voice.enrich(items, state=self.root, account='account_fixture', read_audio=self.audio, **changes)

    def test_saved_wechat_text_wins_without_model_audio_or_cache(self):
        item = message(content={'VoiceTranscript': {'text': '微信原文'}})
        with patch.object(runtime, 'ensure') as install, patch.object(runtime, 'transcribe') as asr:
            self.enrich([item])
        self.assertEqual(item['voiceTranscript']['text'], '微信原文')
        self.assertEqual(item['voiceTranscript']['source'], 'wechat')
        self.assertEqual(item['snippet'], '[voice] 微信原文')
        self.assertFalse(list(self.root.iterdir()))
        install.assert_not_called(); asr.assert_not_called(); self.audio.assert_not_called()

    def test_missing_native_text_is_automatic_and_second_read_reuses_private_cache(self):
        first, second = message(), message()
        with patch.object(runtime, 'ensure', return_value=self.root) as install, patch.object(runtime, 'transcribe', return_value=self.result.copy()) as asr:
            self.enrich([first])
            self.enrich([second])
        install.assert_called_once(); asr.assert_called_once(); self.audio.assert_called_once()
        self.audio.assert_called_with(first['talker'], first['server_id'], self.root / 'bin/ffmpeg')
        self.assertFalse(first['voiceTranscript']['cached']); self.assertTrue(second['voiceTranscript']['cached'])
        self.assertEqual(first['content'], 'Voice')
        cache = voice.cache_path(self.root, 'account_fixture', first)
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cache.parent.stat().st_mode & 0o777, 0o700)

    def test_account_and_conversation_are_part_of_cache_identity(self):
        a = voice.cache_path(self.root, 'account_a', message())
        self.assertNotEqual(a, voice.cache_path(self.root, 'account_b', message()))
        self.assertNotEqual(a, voice.cache_path(self.root, 'account_a', message(talker='other')))

    def test_native_only_and_off_modes_do_not_install_or_transcribe(self):
        with patch.object(runtime, 'ensure') as install:
            native = message(); self.enrich([native], mode='wechat')
            off = message(); self.enrich([off], mode='off')
        self.assertEqual(native['voiceTranscript']['error'], 'wechat_voice_text_not_saved')
        self.assertNotIn('voiceTranscript', off)
        install.assert_not_called(); self.audio.assert_not_called()

    def test_hidden_or_nonvoice_messages_never_resolve_audio(self):
        item = message(content={'Text': '[消息已隐藏]'})
        self.enrich([item])
        self.assertNotIn('voiceTranscript', item); self.audio.assert_not_called()

    def test_failure_is_explicit_preserves_original_and_is_not_cached_as_text(self):
        item = message()
        with patch.object(runtime, 'ensure', return_value=self.root), patch.object(runtime, 'transcribe', side_effect=runtime.VoiceError('voice_no_speech_recognized')):
            self.enrich([item])
        self.assertEqual(item['content'], 'Voice'); self.assertEqual(item['snippet'], '[voice]')
        self.assertEqual(item['voiceTranscript']['status'], 'unavailable')
        self.assertIsNone(item['voiceTranscript']['text'])
        self.assertEqual(item['voiceTranscript']['error'], 'voice_no_speech_recognized')
        self.assertFalse(list(self.root.rglob('*.json')))

    def test_bootstrap_failure_is_attempted_once_per_batch(self):
        items = [message(), message(server_id=123)]
        with patch.object(runtime, 'ensure', side_effect=runtime.VoiceError('voice_runtime_download_failed')) as install:
            self.enrich(items)
        install.assert_called_once(); self.audio.assert_not_called()
        self.assertTrue(all(i['voiceTranscript']['error'] == 'voice_runtime_download_failed' for i in items))

    def test_invalid_selector_does_not_install_or_read_audio(self):
        with patch.object(runtime, 'ensure') as install:
            items = [message(server_id=0), message(talker=None)]
            self.enrich(items)
        install.assert_not_called(); self.audio.assert_not_called()
        self.assertTrue(all(i['voiceTranscript']['status'] == 'unavailable' for i in items))

    def test_malformed_or_public_cache_is_ignored(self):
        for content in ['[]', '{', '{"status":"success","text":"forged"}']:
            path = voice.cache_path(self.root, 'account_fixture', message())
            path.parent.mkdir(exist_ok=True); path.write_text(content); path.chmod(0o600)
            self.assertIsNone(voice.read_cache(path))
        path.chmod(0o644); self.assertIsNone(voice.read_cache(path))

    def test_invalid_mode_fails_before_any_work(self):
        with self.assertRaises(runtime.VoiceError): self.enrich([message()], mode='cloud')
        self.audio.assert_not_called()


class VoiceBridgeTests(unittest.TestCase):
    def test_history_enrichment_preserves_pagination(self):
        page = {'items': [message()], 'paging': {'returned': 1, 'has_more': True}}
        with patch.object(bridge, 'account_args', return_value=['--account', 'fixture']), patch.object(bridge, 'run_wx', return_value=page), patch.object(bridge, 'enrich_voice_messages') as enrich:
            result = bridge.call('wechat_read_messages', {'contact': 'fixture', 'offset': 20})
        enrich.assert_called_once_with(page['items'], {'contact': 'fixture', 'offset': 20})
        self.assertEqual(result['paging']['nextOffset'], 21)

    def test_exact_voice_tool_keeps_64_bit_id_and_rejects_nonvoice(self):
        item = message()
        def enrich(items, args): items[0]['voiceTranscript'] = {'status': 'success', 'text': 'fixture', 'source': 'wechat'}
        with patch.object(bridge, 'paged_query', return_value={'items': [item]}) as query, patch.object(bridge, 'enrich_voice_messages', side_effect=enrich):
            result = bridge.call('wechat_transcribe_voice', {'contact': item['talker'], 'serverId': str(item['server_id'])})
            self.assertEqual(result['serverId'], str(item['server_id']))
            self.assertEqual(query.call_args[0][1]['aroundServerId'], item['server_id'])
            self.assertEqual(query.call_args[0][1]['voiceTranscription'], 'off')
            item['content'] = {'Text': 'hidden'}
            with self.assertRaises(bridge.BridgeError) as error:
                bridge.call('wechat_transcribe_voice', {'contact': item['talker'], 'serverId': str(item['server_id'])})
            self.assertEqual(error.exception.code, 'message_not_voice')

    def test_voice_status_is_read_only_and_has_no_setup_side_effects(self):
        with patch.object(runtime, 'ensure') as install, patch.object(bridge, 'accounts') as accounts, patch.object(bridge, 'run_wx') as query:
            result = bridge.call('wechat_voice_status', {})
        self.assertFalse(result['audioUploaded']); self.assertFalse(result['requiresApiKey'])
        install.assert_not_called(); accounts.assert_not_called(); query.assert_not_called()
        self.assertTrue(next(t for t in bridge.TOOLS if t['name'] == 'wechat_transcribe_voice')['annotations']['readOnlyHint'])

    def test_incremental_receive_enriches_before_returning_new_cursor(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(bridge, 'STATE', Path(folder)), patch.object(bridge, 'accounts', return_value={'account_fixture': Path('/fixture')}), patch.object(bridge, 'run_wx', side_effect=[{'items': []}, {'items': [message()], 'paging': {'has_more': False}}]), patch.object(bridge, 'enrich_voice_messages') as enrich:
            start = bridge.call('wechat_receive_start', {'contact': 'fixture'})
            result = bridge.call('wechat_receive_pull', {'contact': 'fixture', 'cursor': start['cursor']})
        self.assertEqual(result['messages'][0]['server_id'], message()['server_id'])
        enrich.assert_called_once()

    def test_timeline_and_sse_message_batches_are_enriched(self):
        from backends.receive import local_service
        for name, data, args in [('wechat_timeline', {'items': [message()]}, {'since': 1, 'until': 2}),
                                  ('wechat_read_events', {'events': [{'type': 'message', 'messages': [message()]}]}, {'seconds': 1})]:
            with patch.object(bridge, 'account_args', return_value=['--account', 'fixture']), patch.object(bridge, 'database_root'), patch.object(local_service, 'ensure', return_value={}), patch.object(local_service, 'query', return_value=data), patch.object(local_service, 'events', return_value=data), patch.object(bridge, 'enrich_voice_messages') as enrich, patch.object(bridge, 'private_state', return_value=Path('/fixture')):
                bridge.call(name, args)
                enrich.assert_called_once()


class VoiceRuntimeTests(unittest.TestCase):
    def test_archive_paths_cannot_escape_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(runtime.VoiceError): runtime.write_member(root, '../outside', io.BytesIO(b'bad'))
            self.assertFalse((root.parent / 'outside').exists())
            (root / 'link').symlink_to(root.parent)
            with self.assertRaises(runtime.VoiceError): runtime.write_member(root, 'link/escape', io.BytesIO(b'bad'))

    def test_download_requires_pinned_size_and_digest(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(runtime.urllib.request, 'urlopen') as request:
            request.return_value.__enter__.return_value = io.BytesIO(b'forged')
            with self.assertRaises(runtime.VoiceError) as error:
                runtime.download(('https://example.invalid/runtime', 'a' * 64, 6), Path(folder) / 'asset')
            self.assertEqual(error.exception.code, 'voice_runtime_checksum_mismatch')
            self.assertFalse((Path(folder) / 'asset').exists())


if __name__ == '__main__': unittest.main()

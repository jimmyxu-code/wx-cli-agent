"""Attach voice text to normal message reads, without delegating ASR to agents."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import voice_runtime as runtime


def is_voice(message):
    content = message.get('content')
    return content == 'Voice' or isinstance(content, dict) and 'VoiceTranscript' in content


def native_text(message):
    content = message.get('content')
    value = content.get('VoiceTranscript', {}).get('text') if isinstance(content, dict) else None
    return value if isinstance(value, str) and value.strip() and len(value) <= 32000 and '\0' not in value else None


def validate_mode(mode):
    if mode not in ('auto', 'wechat', 'off'):
        raise runtime.VoiceError('voice_transcription_mode_invalid')
    return mode


def cache_path(state, account, message):
    identity = [account, message.get('talker'), str(message.get('server_id')),
                message.get('sort_seq'), message.get('create_time'), runtime.VERSION]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return Path(state) / 'voice-transcripts' / (digest + '.json')


def read_cache(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072 or path.stat().st_mode & 0o077:
            return None
        value = json.loads(path.read_text())
        if isinstance(value, dict) and value.get('status') == 'success' and value.get('source') == 'local_sensevoice' and value.get('model') == runtime.VERSION and isinstance(value.get('text'), str) and value['text'].strip() and len(value['text']) <= 32000 and '\0' not in value['text']:
            return {**value, 'cached': True}
    except (OSError, ValueError):
        pass
    return None


def write_cache(path, result):
    fd, name = tempfile.mkstemp(prefix='.transcript-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(result, stream, ensure_ascii=False)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def enrich(messages, *, state, account, read_audio, mode='auto'):
    validate_mode(mode)
    if mode == 'off':
        return messages
    engine = None
    engine_error = None
    for message in messages:
        if not isinstance(message, dict) or not is_voice(message):
            continue
        text = native_text(message)
        if text is not None:
            result = {'status': 'success', 'text': text, 'source': 'wechat', 'cached': True, 'audioUploaded': False}
        elif mode == 'wechat':
            result = {'status': 'unavailable', 'text': None, 'source': 'wechat',
                      'error': 'wechat_voice_text_not_saved', 'audioUploaded': False}
        else:
            result = None
            try:
                if not isinstance(message.get('talker'), str) or not message['talker'] or not str(message.get('server_id', '')).isdecimal() or int(message['server_id']) <= 0:
                    raise runtime.VoiceError('voice_message_selector_missing')
                path = cache_path(state, account, message)
                runtime.private_directory(path.parent)
                with runtime.locked(path.with_suffix('.lock')):
                    result = read_cache(path)
                    if result is None:
                        if engine_error:
                            raise runtime.VoiceError(engine_error)
                        if engine is None:
                            try:
                                engine = runtime.ensure()
                            except runtime.VoiceError as error:
                                engine_error = error.code
                                raise
                        media = read_audio(message['talker'], int(message['server_id']), engine / 'bin/ffmpeg')
                        result = runtime.transcribe(media['path'], engine)
                        result.update(cached=False, audioSha256=media['sha256'])
                        write_cache(path, result)
            except Exception as error:
                result = {'status': 'unavailable', 'text': None, 'source': 'local_sensevoice',
                          'error': getattr(error, 'code', 'voice_transcription_failed'), 'audioUploaded': False}
        message['voiceTranscript'] = result
        if result['status'] == 'success':
            # Preserve original content/type and identity. Text is also visible
            # in the normal preview that agents already consume.
            message['snippet'] = '[voice] ' + result['text']
    return messages

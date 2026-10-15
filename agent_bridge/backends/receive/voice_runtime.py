"""Pinned, local-only speech runtime. Downloads public software, never audio.

sherpa-onnx: Apache-2.0, https://github.com/k2-fsa/sherpa-onnx
SenseVoice: https://github.com/FunAudioLLM/SenseVoice (model license applies)
FFmpeg distribution/license: https://github.com/imageio/imageio-ffmpeg
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import wave
import zipfile

VERSION = 'sensevoice-int8-20250909-sherpa-1.13.8'
ROOT = Path.home() / 'Library/Caches/wx-cli-agent' / VERSION
ASSETS = {
    'runtime': ('https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.8/sherpa-onnx-v1.13.8-osx-arm64-shared-no-tts.tar.bz2',
                '91b96512c4fa1960f8a9ed5360a6c8dda53a4b5015d0590244f14086a234557a', 18252168),
    'model': ('https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2',
              '7305f7905bfcf77fa0b39388a313f3da35c68d971661a65475b56fb2162c8e63', 165783878),
    'ffmpeg': ('https://files.pythonhosted.org/packages/40/5c/f3d8a657d362cc93b81aab8feda487317da5b5d31c0e1fdfd5e986e55d17/imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl',
               'b1ae3173414b5fc5f538a726c4e48ea97edc0d2cdc11f103afee655c463fa742', 21113891),
}
FILES = ('bin/sherpa-onnx-offline', 'lib/libonnxruntime.dylib',
         'lib/libsherpa-onnx-cxx-api.dylib', 'lib/libsherpa-onnx-c-api.dylib',
         'model/model.int8.onnx', 'model/tokens.txt', 'bin/ffmpeg')


class VoiceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def private_directory(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise VoiceError('voice_cache_path_unsafe')
    path = path.resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


@contextlib.contextmanager
def locked(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def available(root=None):
    root = Path(root or ROOT)
    return all((root / name).is_file() and not (root / name).is_symlink() for name in FILES)


def status():
    return {'automaticInMessageReads': True, 'nativeSavedTextSupported': True,
            'localEngine': VERSION, 'localEngineInstalled': available(),
            'audioUploaded': False, 'requiresApiKey': False, 'requiresGui': False,
            'firstDownloadBytes': sum(a[2] for a in ASSETS.values()),
            'firstDownloadOnly': True, 'languages': ['zh', 'en', 'yue', 'ja', 'ko'],
            'note': 'Prefer saved WeChat text. Missing text is transcribed locally and cached; voiceTranscription=wechat or off avoids automatic model setup.'}


def download(asset, destination):
    url, expected, size = asset
    if destination.is_file() and not destination.is_symlink() and hashlib.sha256(destination.read_bytes()).hexdigest() == expected:
        return
    for attempt in range(3):
        temporary = destination.with_suffix('.part')
        try:
            total, digest = 0, hashlib.sha256()
            with urllib.request.urlopen(url, timeout=45) as source:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    deadline = time.monotonic() + 600
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > size or time.monotonic() > deadline:
                            raise VoiceError('voice_runtime_download_limit')
                        digest.update(block)
                        stream.write(block)
            if total != size or digest.hexdigest() != expected:
                raise VoiceError('voice_runtime_checksum_mismatch')
            os.replace(temporary, destination)
            return
        except (OSError, ValueError):
            if attempt == 2:
                raise VoiceError('voice_runtime_download_failed') from None
        finally:
            temporary.unlink(missing_ok=True)


def write_member(root, name, source, executable=False):
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise VoiceError('voice_runtime_archive_invalid')
    destination = root / str(relative)
    if not destination.resolve().is_relative_to(root.resolve()):
        raise VoiceError('voice_runtime_archive_invalid')
    private_directory(destination.parent)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o700 if executable else 0o600)
    os.fchmod(fd, 0o700 if executable else 0o600)
    with os.fdopen(fd, 'wb') as stream:
        shutil.copyfileobj(source, stream)


def unpack(archive, root, kind):
    if kind == 'ffmpeg':
        with zipfile.ZipFile(archive) as source:
            found = [n for n in source.namelist() if n.startswith('imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-') and '/' not in n[len('imageio_ffmpeg/binaries/'):]]
            if len(found) != 1:
                raise VoiceError('voice_runtime_archive_invalid')
            with source.open(found[0]) as stream:
                write_member(root, 'bin/ffmpeg', stream, True)
        return
    with tarfile.open(archive) as source:
        for item in source:
            parts = PurePosixPath(item.name).parts
            if not item.isfile() or len(parts) < 2:
                continue
            relative = '/'.join(parts[1:])
            if kind == 'model' and relative in ('model.int8.onnx', 'tokens.txt'):
                relative = 'model/' + relative
            elif kind == 'runtime' and (relative == 'bin/sherpa-onnx-offline' or relative.startswith('lib/') and relative.endswith('.dylib')):
                pass
            else:
                continue
            with source.extractfile(item) as stream:
                write_member(root, relative, stream, kind == 'runtime')


def ensure():
    if platform.system() != 'Darwin' or platform.machine() not in ('arm64', 'aarch64'):
        raise VoiceError('voice_runtime_platform_unsupported')
    root = private_directory(ROOT)
    with locked(root / 'install.lock'):
        if not available(root):
            downloads = private_directory(root / 'downloads')
            for name, asset in ASSETS.items():
                archive = downloads / (name + '.archive')
                download(asset, archive)
                unpack(archive, root, name)
            if not available(root):
                raise VoiceError('voice_runtime_install_incomplete')
    return root


def transcribe(audio, root):
    with tempfile.TemporaryDirectory(prefix='voice-', dir=root) as directory:
        wav = Path(directory) / 'input.wav'
        try:
            converted = subprocess.run([str(root / 'bin/ffmpeg'), '-nostdin', '-v', 'error', '-i', str(audio),
                                        '-t', '121', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(wav)],
                                       capture_output=True, timeout=30)
            if converted.returncode:
                raise VoiceError('voice_audio_decode_failed')
            with wave.open(str(wav), 'rb') as stream:
                duration = stream.getnframes() / stream.getframerate()
            if not 0 < duration <= 120:
                raise VoiceError('voice_duration_limit')
            result = subprocess.run([str(root / 'bin/sherpa-onnx-offline'),
                                     '--sense-voice-model=' + str(root / 'model/model.int8.onnx'),
                                     '--tokens=' + str(root / 'model/tokens.txt'),
                                     '--sense-voice-use-itn=true', '--num-threads=2', str(wav)],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise VoiceError('voice_recognition_failed')
            data = json.loads(result.stdout)
            text = data.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 32000 or '\0' in text:
                raise VoiceError('voice_no_speech_recognized')
            return {'status': 'success', 'text': text.strip(), 'source': 'local_sensevoice',
                    'model': VERSION, 'language': data.get('lang', '').replace('<|', '').replace('|>', ''),
                    'durationMs': round(duration * 1000), 'audioUploaded': False}
        except subprocess.TimeoutExpired:
            raise VoiceError('voice_transcription_timeout') from None
        except (OSError, ValueError, wave.Error):
            raise VoiceError('voice_transcription_failed') from None

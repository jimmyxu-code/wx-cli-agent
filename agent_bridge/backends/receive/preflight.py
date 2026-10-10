"""Read-only Mac key-path preflight. Never attach, sign, launch or read key contents."""
import json
import platform
import plistlib
import subprocess
from pathlib import Path


def inspect(app=Path('/Applications/WeChat.app'), home=None, runner=subprocess.run):
    home = Path(home) if home else Path.home()
    version = build = None
    try:
        with (Path(app) / 'Contents/Info.plist').open('rb') as stream:
            info = plistlib.load(stream)
        version = info.get('CFBundleShortVersionString')
        build = info.get('CFBundleVersion')
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    try:
        result = runner(['/usr/bin/csrutil', 'status'], capture_output=True, text=True, timeout=5)
        output = result.stdout.lower()
        sip = 'enabled' if 'status: enabled' in output else 'disabled' if 'status: disabled' in output else 'unknown'
    except (OSError, subprocess.SubprocessError):
        sip = 'unknown'
    root = home / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
    try:
        accounts = sum(p.is_dir() and (p/'db_storage').is_dir() for p in root.iterdir())
    except OSError:
        accounts = None
    key_paths = [home/'Library/Application Support/wx-cli/config/keys.toml',
                 home/'Library/Application Support/wx-cli/keys.toml', home/'.config/wx-cli/keys.toml']
    key_present = any(p.is_file() for p in key_paths)
    return {
        'platform': platform.system(), 'architecture': platform.machine(),
        'wechatVersion': version, 'wechatBuild': build, 'sip': sip,
        'accountDirectoryCount': accounts, 'keyFilePresent': key_present,
        'keyValidationPerformed': False, 'historyReadableVerified': False,
        'routes': {
            'wxCliOriginalSignature': 'blocked_sip' if sip == 'enabled' else 'not_verified',
            'traceMemo413': 'blocked_sip' if sip == 'enabled' else 'not_verified',
            'temporaryDebugCopy': 'requires_explicit_approval_and_version_validation'
        },
        'performed': ['plist_metadata', 'sip_status', 'directory_count', 'key_file_existence'],
        'modifications': []
    }


if __name__ == '__main__':
    print(json.dumps(inspect(), ensure_ascii=False))

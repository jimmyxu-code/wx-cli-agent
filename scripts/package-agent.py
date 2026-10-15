#!/usr/bin/env python3
"""Build an agent-ready macOS archive from an explicit source allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile

REPO = Path(__file__).resolve().parents[1]


def package(repo, binary, output, version):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', version):
        raise ValueError('invalid version')
    binary = Path(binary)
    if not binary.is_file() or binary.is_symlink():
        raise ValueError('explicit regular wx-cli binary required')
    files = [repo / name for name in ('wx', 'README.md', 'SKILL.md', 'AGENTS.md', 'LICENSE', 'docs/UPSTREAM_README.md', 'scripts/package-agent.py')]
    for folder in ('agent_bridge', 'experimental/native_send'):
        for path in (repo / folder).rglob('*'):
            relative = path.relative_to(repo)
            if any(part in ('.state', '__pycache__', 'bin') for part in relative.parts):
                continue
            if path.is_file() and not path.is_symlink() and (path.suffix in ('.py', '.sh', '.md', '.cpp', '.h') or path.name in ('LICENSE', 'SOURCE.json', 'runtime-release.json')):
                files.append(path)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'w:gz') as archive:
        for path in sorted(set(files)):
            if not path.is_file() or path.is_symlink():
                raise ValueError('required source file missing')
            archive.add(path, arcname='wx-cli-agent/' + str(path.relative_to(repo)), recursive=False)
        archive.add(binary, arcname='wx-cli-agent/agent_bridge/bin/wx-cli', recursive=False)
    summary = {'schemaVersion': 1, 'version': version, 'platform': 'macos-arm64',
               'archive': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
               'runtimeSha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
               'privateStateIncluded': False}
    checksum = output.with_name(output.name + '.sha256')
    checksum.write_text(summary['sha256'] + '  ' + output.name + '\n')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(package(REPO, args.binary, args.output, args.version)))

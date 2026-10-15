"""Resolve files inside a merged record by parent message + nested item path.

Locates existing originals or downloads them through the client's CDN route
cache. CDN identifiers/keys remain private. Original inner message IDs are
metadata, never a lookup in the current parent conversation.
"""
import hashlib
import html
import itertools
import os
from pathlib import Path
import re
import stat
import tempfile
import xml.etree.ElementTree as ET

MAX_XML = 4 * 1024 * 1024
MAX_FILE = 256 * 1024 * 1024


class AttachmentError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def parse_xml(value):
    if not isinstance(value, str) or len(value.encode('utf8')) > MAX_XML:
        raise AttachmentError('merged_record_xml_invalid')
    for _ in range(3):
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', value, re.I):
            raise AttachmentError('merged_record_xml_invalid')
        try:
            return ET.fromstring(value)
        except ET.ParseError:
            changed = html.unescape(value)
            if changed == value:
                break
            value = changed
    raise AttachmentError('merged_record_xml_invalid')


def tag(element):
    return element.tag.rsplit('}', 1)[-1]


def child_text(element, name):
    return next((n.text or '' for n in element if tag(n) == name), '')


def record_root(element):
    if tag(element) == 'recordinfo':
        return element
    for node in element.iter():
        if tag(node) == 'recordinfo':
            return node
        if tag(node) in ('recorditem', 'recordxml') and node.text:
            parsed = parse_xml(node.text)
            root = record_root(parsed)
            if root is not None:
                return root
    return None


def record_items(record):
    datalist = next((n for n in record if tag(n) == 'datalist'), None)
    return [n for n in datalist if tag(n) == 'dataitem'] if datalist is not None else []


def descriptor(item, path):
    title = child_text(item, 'datatitle')
    digest = child_text(item, 'fullmd5').lower()
    size = child_text(item, 'datasize')
    if not title or title in ('.', '..') or any(c in title for c in ('/', '\\', '\0')) or len(title) > 255:
        raise AttachmentError('merged_file_name_invalid')
    if not re.fullmatch('[0-9a-f]{32}', digest) or not size.isdecimal() or not 0 < int(size) <= MAX_FILE:
        raise AttachmentError('merged_file_integrity_metadata_missing')
    source = next((n for n in item if tag(n) == 'dataitemsource'), None)
    original = child_text(item, 'fromnewmsgid') or (child_text(source, 'fromnewmsgid') if source is not None else '')
    # Return only file identity fields. No raw XML, CDN key, token or URL.
    return {'itemPath': path, 'dataId': item.get('dataid', ''), 'fileName': title,
            'fileFormat': child_text(item, 'datafmt'), 'expectedBytes': int(size), 'expectedMd5': digest,
            'originalMessageId': original or None,
            'clientDownloadReferencePresent': bool(child_text(item, 'cdndataurl'))}


def file_items(message):
    content = message.get('content', {}).get('MergedMessages')
    if not isinstance(content, dict):
        raise AttachmentError('parent_message_not_merged_record')
    root = record_root(parse_xml(content.get('raw_xml', '')))
    if root is None:
        raise AttachmentError('merged_record_items_missing')
    output = []
    def walk(record, parent_path, depth):
        if depth > 8:
            raise AttachmentError('merged_record_nesting_limit')
        for index, item in enumerate(record_items(record)):
            if len(output) >= 2048:
                raise AttachmentError('merged_record_item_limit')
            path = parent_path + [index]
            if item.get('datatype') == '8':
                output.append(descriptor(item, path))
            elif item.get('datatype') == '17':
                nested = record_root(item)
                if nested is not None:
                    walk(nested, path, depth + 1)
    walk(root, [], 0)
    return output


def select(items, item_path=None, data_id=None):
    if (item_path is None) == (data_id is None):
        raise AttachmentError('merged_attachment_selector_required')
    if item_path is not None and (not isinstance(item_path, list) or not item_path or len(item_path) > 9 or
                                 any(type(n) is not int or not 0 <= n < 2048 for n in item_path)):
        raise AttachmentError('merged_attachment_selector_invalid')
    found = [item for item in items if item['itemPath'] == item_path] if item_path is not None else [item for item in items if item['dataId'] == data_id]
    if len(found) != 1:
        raise AttachmentError('merged_attachment_not_found_or_ambiguous')
    return found[0]


def parent_identity(message, contact):
    return {'contact': contact, 'serverId': str(message['server_id']), 'sortSeq': message['sort_seq']}


def hashes(stream):
    md5, sha, size = hashlib.md5(), hashlib.sha256(), 0
    while True:
        block = stream.read(1024 * 1024)
        if not block:
            break
        size += len(block)
        if size > MAX_FILE:
            raise AttachmentError('merged_file_size_limit')
        md5.update(block)
        sha.update(block)
    return size, md5.hexdigest(), sha.hexdigest()


def export_target(account_root, media_root, message, contact, item):
    parent = parent_identity(message, contact)
    identity = str(Path(account_root).resolve()) + contact + parent['serverId'] + str(item['itemPath']) + item['expectedMd5']
    directory = Path(media_root) / ('merged-' + hashlib.sha256(identity.encode()).hexdigest()[:24])
    if directory.is_symlink() or directory.resolve() != directory:
        raise AttachmentError('merged_export_scope')
    return directory / item['fileName']


def download_reference(message, item):
    root = record_root(parse_xml(message['content']['MergedMessages']['raw_xml']))
    for depth, index in enumerate(item['itemPath']):
        nodes = record_items(root)
        if index >= len(nodes):
            raise AttachmentError('merged_attachment_not_found_or_ambiguous')
        node = nodes[index]
        if depth + 1 < len(item['itemPath']):
            root = record_root(node)
            if node.get('datatype') != '17' or root is None:
                raise AttachmentError('merged_attachment_not_found_or_ambiguous')
    if node.get('datatype') != '8' or descriptor(node, item['itemPath']) != item:
        raise AttachmentError('merged_attachment_not_found_or_ambiguous')
    file_type = child_text(node, 'filetype')
    return {'fileId': child_text(node, 'cdndataurl'), 'aesKey': child_text(node, 'cdndatakey'),
            'fileType': int(file_type) if file_type.isdecimal() else 5}


def acquire(account_root, media_root, message, contact, item, *, allow_download=True, force_download=False):
    if not force_download:
        result = export(account_root, media_root, message, contact, item)
        if result['originalFileObtained'] or not allow_download:
            return result
    from . import cdn_download
    target = export_target(account_root, media_root, message, contact, item)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.is_symlink() or target.parent.resolve() != target.parent:
        raise AttachmentError('merged_export_scope')
    os.chmod(target.parent, 0o700)
    # account = Documents/xwechat_files/<account>; the shared route cache is
    # maintained by WeChat, outside the message databases and attachment tree.
    cache = Path(account_root).resolve().parent.parent / 'app_data/net/cdncomm/cdninfo_new.cache'
    try:
        downloaded = cdn_download.download(cache, download_reference(message, item), target,
                                           item['expectedBytes'], item['expectedMd5'])
        return {'status': 'success', 'originalFileObtained': True,
                'parent': parent_identity(message, contact), 'item': item, 'path': str(target),
                'contentType': 'application/pdf' if item['fileFormat'].lower() == 'pdf' else 'application/octet-stream',
                'backend': 'merged_record_cdn_original', **downloaded}
    except cdn_download.DownloadError as error:
        result = {'status': 'unavailable', 'originalFileObtained': False,
                  'parent': parent_identity(message, contact), 'item': item,
                  'backend': 'merged_record_cdn_original', 'error': error.code,
                  'networkDownloadPerformed': False, 'downloadAttempted': True,
                  'innerIdIsParentConversationMessageId': False}
        if error.retcode is not None:
            result['cdnRetcode'] = error.retcode
        if error.code in ('cdn_cache_missing', 'cdn_route_missing', 'cdn_auth_rejected'):
            result['nextActions'] = ['Open the installed WeChat client and ensure it is logged in and connected, then retry this JSON call. No file click is required.']
        else:
            result['nextActions'] = ['Retry the exact JSON download for a transient network error; inspect the error code if it persists.']
        return result


def candidates(account_root, item):
    root = Path(account_root).resolve(strict=True)
    denied = []
    for base, directories, files in os.walk(root, followlinks=False, onerror=lambda error: denied.append(type(error).__name__)):
        directories[:] = [name for name in directories if name not in ('db_storage', '.state') and not (Path(base) / name).is_symlink()]
        for name in files:
            path = Path(base) / name
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and (info.st_size == item['expectedBytes'] or name == item['fileName']):
                    yield path
            except OSError:
                denied.append('unreadable')
    if denied:
        raise AttachmentError('merged_local_scan_incomplete')


def export(account_root, media_root, message, contact, item):
    parent = parent_identity(message, contact)
    target = export_target(account_root, media_root, message, contact, item)
    directory = target.parent
    mismatches = 0
    saved = [target] if target.is_file() and not target.is_symlink() else []
    for path in itertools.chain(saved, candidates(account_root, item)):
        descriptor_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor_fd, 'rb') as stream:
            size, md5, sha256 = hashes(stream)
        if size != item['expectedBytes'] or md5 != item['expectedMd5']:
            if path.name == item['fileName']:mismatches += 1
            continue
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.resolve() != directory:
            raise AttachmentError('merged_export_scope')
        os.chmod(directory, 0o700)
        fd, temporary_name = tempfile.mkstemp(prefix='.original-', dir=directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, 'wb') as output:
                source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(source_fd, 'rb') as source:
                    copied = 0
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:break
                        copied += len(block)
                        if copied > item['expectedBytes']:
                            raise AttachmentError('merged_file_changed_during_export')
                        output.write(block)
                output.flush();os.fsync(output.fileno())
            with temporary.open('rb') as stream:
                actual_size, actual_md5, actual_sha = hashes(stream)
            if (actual_size, actual_md5) != (item['expectedBytes'], item['expectedMd5']):
                raise AttachmentError('merged_file_changed_during_export')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {'status': 'success', 'originalFileObtained': True, 'parent': parent, 'item': item,
                'path': str(target), 'bytes': actual_size, 'md5': actual_md5, 'sha256': actual_sha,
                'sizeVerified': True, 'md5Verified': True, 'contentType': 'application/pdf' if item['fileFormat'].lower() == 'pdf' else 'application/octet-stream',
                'backend': 'merged_record_local_original', 'networkDownloadPerformed': False}
    return {'status': 'unavailable', 'originalFileObtained': False, 'parent': parent, 'item': item,
            'error': 'merged_file_integrity_mismatch' if mismatches else 'merged_file_not_downloaded',
            'backend': 'merged_record_local_original', 'networkDownloadPerformed': False,
            'nextActions': ['Open this exact merged record in the logged-in WeChat client.',
                            'Open/download this file entry, then repeat the same JSON call.'],
            'innerIdIsParentConversationMessageId': False}

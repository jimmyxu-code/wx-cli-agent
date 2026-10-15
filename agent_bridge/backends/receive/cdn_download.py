"""Bounded WeChat C2C CDN reads using the client's private route cache.

No window, process injection, account changes, or message submission. Credentials
stay in memory. Only the original size/MD5-verified plaintext is published.
"""
import ctypes
import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile
import time

CHUNK = 1024 * 1024
MAX_FILE = 256 * 1024 * 1024
MAX_CACHE = 2 * 1024 * 1024


class DownloadError(Exception):
    def __init__(self, code, retcode=None):
        super().__init__(code)
        self.code, self.retcode = code, retcode


def varint(data, at):
    number = 0
    for shift in range(0, 70, 7):
        if at >= len(data):
            raise DownloadError('cdn_cache_invalid')
        byte = data[at]; at += 1
        if shift == 63 and byte > 1:
            raise DownloadError('cdn_cache_invalid')
        number |= (byte & 127) << shift
        if not byte & 128:
            return number, at
    raise DownloadError('cdn_cache_invalid')


def protobuf(data):
    fields, at = {}, 0
    while at < len(data):
        key, at = varint(data, at)
        field, wire = key >> 3, key & 7
        if not 0 < field < 2048:
            raise DownloadError('cdn_cache_invalid')
        if wire == 0:
            value, at = varint(data, at)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, at = varint(data, at)
            else:
                size = {1: 8, 5: 4}[wire]
            if size > len(data) - at:
                raise DownloadError('cdn_cache_invalid')
            value = data[at:at + size]; at += size
        else:
            raise DownloadError('cdn_cache_invalid')
        fields.setdefault(field, []).append(value)
        if sum(map(len, fields.values())) > 4096:
            raise DownloadError('cdn_cache_invalid')
    return fields


def one(fields, field, expected):
    values = fields.get(field, [])
    if len(values) != 1 or type(values[0]) is not expected:
        raise DownloadError('cdn_cache_invalid')
    return values[0]


def unsigned_uin(value):
    # protobuf int32 negatives occupy ten varint bytes. Do not send that uint64
    # representation as weixinnum: the CDN rejects its auth key (-5103011).
    if type(value) is not int or not (0 < value <= 0xffffffff or
                                    0xffffffff80000000 <= value <= 0xffffffffffffffff):
        raise DownloadError('cdn_cache_invalid')
    return value & 0xffffffff


def routes(cache_path):
    try:
        fd = os.open(cache_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > MAX_CACHE:
                raise DownloadError('cdn_cache_invalid')
            networks = protobuf(stream.read(MAX_CACHE + 1)).get(1, [])
    except OSError:
        raise DownloadError('cdn_cache_missing') from None
    output, seen = [], set()
    # Network entries can retain an old login. Try bounded alternate credentials
    # after an auth rejection, without asking the user to log in preemptively.
    for raw in reversed(networks):
        network = protobuf(raw)
        for group in (7, 8):
            for encoded in network.get(group, []):
                config = protobuf(encoded)
                version = one(config, 1, int)
                uin = unsigned_uin(one(config, 2, int))
                for encoded_route in config.get(7, []):
                    route = protobuf(encoded_route)
                    if route.get(1) != [1]:
                        continue  # The ordinary C2C route; other IDs are other services.
                    auth = one(route, 8, bytes)
                    if not 16 <= len(auth) <= 4096 or 443 not in route.get(7, []):
                        continue
                    for address in route.get(3, []):
                        if type(address) is not int:
                            continue
                        ip = ipaddress.IPv4Address(struct.pack('<I', address & 0xffffffff))
                        if not ip.is_global:
                            continue
                        identity = (str(ip), uin, hashlib.sha256(auth).digest())
                        if identity not in seen:
                            output.append({'ip': str(ip), 'uin': uin, 'version': version, 'auth': auth})
                            seen.add(identity)
                        if len(output) == 8:
                            return output
    if not output:
        raise DownloadError('cdn_route_missing')
    return output


def pack(fields):
    output = bytearray()
    for key, value in fields.items():
        for data in (key.encode('ascii'), value if isinstance(value, bytes) else str(value).encode('ascii')):
            output.extend(struct.pack('>I', len(data))); output.extend(data)
    return bytes(output)


def unpack(data):
    fields, at = {}, 0
    while at < len(data):
        pair = []
        for _ in range(2):
            if len(data) - at < 4:
                raise DownloadError('cdn_response_invalid')
            size = struct.unpack_from('>I', data, at)[0]; at += 4
            if size > len(data) - at:
                raise DownloadError('cdn_response_invalid')
            pair.append(data[at:at + size]); at += size
        if not re.fullmatch(rb'[A-Za-z0-9_-]{1,64}', pair[0]):
            raise DownloadError('cdn_response_invalid')
        key = pair[0].decode('ascii')
        if key in fields or len(fields) >= 128:
            raise DownloadError('cdn_response_invalid')
        fields[key] = pair[1]
    return fields


def number(fields, key):
    value = fields.get(key, b'')
    if not re.fullmatch(rb'-?[0-9]{1,20}', value):
        raise DownloadError('cdn_response_invalid')
    return int(value)


def fetch_range(route, file_id, file_type, start, end, sequence, timeout):
    fields = {'ver': route['version'], 'weixinnum': route['uin'], 'seq': sequence,
              'clientversion': 0x14010d12, 'clientostype': 'MacOS', 'authkey': route['auth'],
              'nettype': 1, 'acceptdupack': 1, 'safeproto': 1, 'filetype': file_type,
              'wxchattype': 0, 'fileid': file_id, 'lastretcode': 0, 'ipseq': 0,
              'wxmsgflag': b'', 'wxautostart': 0, 'downpicformat': 1,
              'largesvideo': 0, 'sourceflag': 0, 'rangestart': start, 'rangeend': end}
    # This is the client's C2C HTTP protocol on its cached port 443. The CDN file
    # bytes remain AES encrypted; endpoints and tokens never come from chat URLs.
    connection = http.client.HTTPConnection(route['ip'], 443, timeout=timeout)
    try:
        connection.request('POST', '/download', pack(fields),
                           {'Content-Type': 'application/octet-stream',
                            'User-Agent': 'MicroMessenger Client', 'Accept': '*/*', 'Connection': 'close'})
        response = connection.getresponse()
        data = response.read(CHUNK + 8193)
        if len(data) > CHUNK + 8192:
            raise DownloadError('cdn_response_size_limit')
        decoded = unpack(data)
        retcode = number(decoded, 'retcode')
        if retcode:
            raise DownloadError('cdn_auth_rejected' if retcode == -5103011 else 'cdn_download_rejected', retcode)
        if response.status != 200:
            raise DownloadError('cdn_http_error')
        if number(decoded, 'seq') != sequence:
            raise DownloadError('cdn_response_invalid')
        return decoded
    except (OSError, http.client.HTTPException):
        raise DownloadError('cdn_network_error') from None
    finally:
        connection.close()


def decrypt_file(source, target, key, expected_size, expected_md5):
    if sys.platform != 'darwin':
        raise DownloadError('cdn_aes_backend_unavailable')
    try:
        library = ctypes.CDLL('/usr/lib/system/libcommonCrypto.dylib')
    except OSError:
        raise DownloadError('cdn_aes_backend_unavailable') from None
    library.CCCryptorCreate.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_void_p)]
    library.CCCryptorUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    library.CCCryptorFinal.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.POINTER(ctypes.c_size_t)]
    library.CCCryptorRelease.argtypes = [ctypes.c_void_p]
    context = ctypes.c_void_p()
    key_buffer = ctypes.create_string_buffer(key)
    if library.CCCryptorCreate(1, 0, 3, key_buffer, len(key), None, ctypes.byref(context)):
        raise DownloadError('cdn_decrypt_failed')
    size, md5, sha256 = 0, hashlib.md5(), hashlib.sha256()
    try:
        with source.open('rb') as incoming, target.open('wb') as outgoing:
            while True:
                data = incoming.read(CHUNK)
                buffer = ctypes.create_string_buffer(len(data) + 16)
                written = ctypes.c_size_t()
                result = (library.CCCryptorUpdate(context, data, len(data), buffer, len(buffer), ctypes.byref(written))
                          if data else library.CCCryptorFinal(context, buffer, len(buffer), ctypes.byref(written)))
                if result:
                    raise DownloadError('cdn_decrypt_failed')
                plaintext = buffer.raw[:written.value]
                size += len(plaintext)
                if size > expected_size:
                    raise DownloadError('cdn_file_integrity_mismatch')
                md5.update(plaintext); sha256.update(plaintext); outgoing.write(plaintext)
                if not data:
                    break
            outgoing.flush(); os.fsync(outgoing.fileno())
    finally:
        library.CCCryptorRelease(context)
    if size != expected_size or md5.hexdigest() != expected_md5:
        raise DownloadError('cdn_file_integrity_mismatch')
    return {'bytes': size, 'md5': md5.hexdigest(), 'sha256': sha256.hexdigest()}


def download(cache_path, reference, target, expected_size, expected_md5):
    if (type(expected_size) is not int or not 0 < expected_size <= MAX_FILE or
            not re.fullmatch('[0-9a-f]{32}', expected_md5) or
            not re.fullmatch('[0-9a-fA-F]{32}', reference.get('aesKey', '')) or
            not re.fullmatch('[0-9a-fA-F]{32,2048}', reference.get('fileId', '')) or
            reference.get('fileType') != 5):
        raise DownloadError('cdn_reference_invalid')
    selected_routes = routes(cache_path)
    encrypted_size = (expected_size // 16 + 1) * 16
    deadline = time.monotonic() + 90
    temporaries = []
    try:
        for prefix in ('.cipher-', '.plaintext-'):
            fd, name = tempfile.mkstemp(prefix=prefix, dir=target.parent)
            os.close(fd); temporaries.append(Path(name))
        cipher, plaintext = temporaries
        last_error = DownloadError('cdn_route_missing')
        for route in selected_routes:
            offset, sequence, requests = 0, 1, 0
            try:
                with cipher.open('wb') as stream:
                    while offset < encrypted_size:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise DownloadError('cdn_download_timeout')
                        end = min(offset + CHUNK, encrypted_size) - 1
                        response = fetch_range(route, reference['fileId'], 5, offset, end, sequence, min(10, remaining))
                        data = response.get('filedata', b'')
                        if (number(response, 'totalsize') != encrypted_size or
                                number(response, 'rangestart') != offset or
                                number(response, 'rangeend') != offset + len(data) - 1 or
                                not 0 < len(data) <= end - offset + 1):
                            raise DownloadError('cdn_range_invalid')
                        stream.write(data); offset += len(data); sequence += 1; requests += 1
                    stream.flush(); os.fsync(stream.fileno())
                result = decrypt_file(cipher, plaintext, bytes.fromhex(reference['aesKey']), expected_size, expected_md5)
                os.replace(plaintext, target)
                return {**result, 'encryptedBytesDownloaded': offset, 'downloadRequests': requests,
                        'networkDownloadPerformed': True, 'sizeVerified': True, 'md5Verified': True}
            except DownloadError as error:
                last_error = error
                if error.code not in ('cdn_auth_rejected', 'cdn_network_error'):
                    raise
        raise last_error
    finally:
        for path in temporaries:
            path.unlink(missing_ok=True)

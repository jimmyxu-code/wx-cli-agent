"""Use the original wx-cli loopback REST service for timeline/media/SSE reads."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


class ServiceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ServiceError('local_service_redirect_rejected')


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request(config, route, params=None, timeout=60, headers=None):
    if route not in ('health', 'timeline', 'media', 'events', 'messages', 'contacts', 'sessions', 'search'):
        raise ServiceError('local_service_route_rejected')
    port = config.get('port')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ServiceError('local_service_config_invalid')
    url = 'http://127.0.0.1:'+str(port)+'/api/v1/'+route
    if params:
        url += '?'+urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer '+config['token'], **(headers or {})})
    try:
        return OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        # Server error bodies may contain private account IDs and filenames.
        raise ServiceError('local_media_unavailable' if route=='media' and error.code==404
                           else 'local_service_http_'+str(error.code))
    except (OSError, urllib.error.URLError):
        raise ServiceError('local_service_unreachable')


def ensure(state, scoped_args, run_process, write_private):
    account = scoped_args[-1]
    root = Path(state)/('local-service-'+hashlib.sha256(account.encode()).hexdigest()[:8])
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    path = root/'connection.json'
    if path.is_file():
        if path.stat().st_mode & 0o077:
            raise ServiceError('unsafe_local_service_config')
        config = json.loads(path.read_text())
    else:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0));port = sock.getsockname()[1]
        config = {'port':port, 'token':secrets.token_urlsafe(32)}
        write_private(path, config)
    try:
        with request(config, 'health', timeout=2) as response:
            health = json.load(response)
        if health.get('status') == 'ok' or health.get('ready') is True:
            return config
    except ServiceError:
        pass
    run_process(['server', 'run', '--runtime-root', str(root), '--host', '127.0.0.1',
                 '--port', str(config['port']), '--token', config['token'], '--poll']+scoped_args)
    with request(config, 'health', timeout=3) as response:
        health = json.load(response)
    if health.get('status') != 'ok' and health.get('ready') is not True:
        raise ServiceError('local_service_not_ready')
    return config


def query(config, route, params):
    with request(config, route, params) as response:
        return json.load(response)


def media(config, params, state):
    destination = Path(state)/'media'
    destination.mkdir(mode=0o700, exist_ok=True)
    os.chmod(destination, 0o700)
    with request(config, 'media', params) as response:
        mime = response.headers.get('Content-Type', 'application/octet-stream').split(';')[0]
        ext = {'image/jpeg':'.jpg', 'image/png':'.png', 'image/webp':'.webp', 'image/gif':'.gif',
               'video/mp4':'.mp4', 'audio/ogg':'.ogg', 'audio/mpeg':'.mp3', 'application/pdf':'.pdf'}.get(mime, '.bin')
        path = destination/(secrets.token_hex(16)+ext)
        size = 0;digest = hashlib.sha256()
        try:
            fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                while True:
                    chunk = response.read(1024*1024)
                    if not chunk:break
                    output.write(chunk);digest.update(chunk);size+=len(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise ServiceError('local_media_read_failed')
        return {'path':str(path), 'bytes':size, 'sha256':digest.hexdigest(), 'contentType':mime,
                'quality':response.headers.get('X-Wechat-Media-Quality'), 'backend':'original_wx_cli_media'}


def events(config, seconds):
    if type(seconds) is not int or not 1 <= seconds <= 30:
        raise ServiceError('invalid_argument')
    deadline = time.monotonic()+seconds
    items = [];event = [];limited = False
    with request(config, 'events', timeout=seconds+2) as response:
        # The upstream sends a heartbeat at least every 15 seconds. A socket
        # timeout ends a quiet interval; it must not be represented as a gap-free stream.
        while time.monotonic()<deadline:
            try:
                raw = response.readline(1024*1024)
                if not raw:break
                line = raw.decode('utf-8').rstrip('\r\n')
            except (OSError, TimeoutError):break
            if line.startswith('data:'):
                event.append(line[5:].lstrip())
            elif not line and event:
                try:items.append(json.loads('\n'.join(event)))
                except ValueError:raise ServiceError('invalid_local_event')
                event=[]
                if len(items)>=100:limited=True;break
    return {'events':items, 'backend':'original_wx_cli_sse', 'truncated':limited,
            'completeMessageStream':False,
            'note':'Live subscription only. Reconnect gaps must be filled using timeline/history; upstream polling can coalesce events.'}

#!/usr/bin/env python3
"""Local-only WeChat MCP/CLI bridge. No chat or credential logging."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
STATE = ROOT / '.state'
WX = os.environ.get('WX_CLI_BINARY', str(ROOT / 'bin/wx-cli'))
NATIVE = os.environ.get('WECHAT_NATIVE_BINARY', str(ROOT / 'native/WeChatBridge.app/Contents/MacOS/wechat-native'))
READ_ONLY_MODE = os.environ.get('WX_BRIDGE_READ_ONLY') == '1'
NATIVE_SEND_MODE = False


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def private_state():
    STATE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STATE, 0o700)
    return STATE


def accounts():
    root = Path.home() / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
    try:
        return {('account_' + hashlib.sha256(p.name.encode()).hexdigest()[:8]): p
                for p in sorted(root.iterdir()) if not p.is_symlink() and p.is_dir()
                and (p / 'db_storage').is_dir()} if root.exists() else {}
    except PermissionError:
        raise BridgeError('wechat_data_permission', 'macOS has denied access to the WeChat data container. Enable this app\'s WeChat access under Files and Folders.')


def account_args(alias=None):
    if not alias:
        found_accounts = accounts()
        if len(found_accounts) != 1:
            raise BridgeError('account_selection_required', 'Select one local account alias explicitly.')
        return ['--account', next(iter(found_accounts.values())).name]
    found = accounts().get(alias)
    if found is None:
        raise BridgeError('unknown_account', 'Account alias was not found on this Mac.')
    return ['--account', found.name]


def wx_environment():
    environment = dict(os.environ)
    if not environment.get('WX_CLI_CACHE_ROOT'):
        environment['WX_CLI_CACHE_ROOT'] = str(private_state() / 'database-cache')
    if not environment.get('FFMPEG_PATH'):
        candidates=sorted((STATE/'media-runtime/imageio_ffmpeg/binaries').glob('ffmpeg-macos-aarch64-*'))
        if candidates:environment['FFMPEG_PATH']=str(candidates[0])
    return environment


def validate_database_output(stderr, envelope=None):
    """A successful exit may still omit WAL changes or unreadable message shards."""
    cache_failures = ('WAL skipped (', 'WAL content err ', 'WAL IO err ',
                      'decrypt err ', 'mkdir err ', 'mtime marker err ', 'copy err ')
    if any(line.strip().startswith(cache_failures) for line in stderr.splitlines()) or re.search(
            r'Cache: \d+ decrypted, \d+ cached, [1-9]\d* errors,', stderr):
        raise BridgeError('database_cache_incomplete',
                          'Database or WAL refresh was incomplete. No receive cursor was advanced; private details withheld.')
    skipped = envelope.get('stats', {}).get('skipped', 0) if isinstance(envelope, dict) else 0
    shards = envelope.get('stats', {}).get('shard_warnings', []) if isinstance(envelope, dict) else []
    if shards or any(line.strip().startswith('warning: shard ') for line in stderr.splitlines()) or (
            isinstance(skipped, int) and skipped > 0) or re.search(
            r'warning: [1-9]\d* messages skipped \(decode error\)', stderr):
        raise BridgeError('message_query_incomplete',
                          'Some message rows or shards could not be read. No receive cursor was advanced; private details withheld.')


def run_wx_process(argv):
    if not Path(WX).is_file():
        raise BridgeError('cli_missing', 'Configure WX_CLI_BINARY with the local wx-cli executable.')
    try:
        result = subprocess.run([WX] + argv, capture_output=True, text=True, timeout=60,
                                preexec_fn=lambda: os.umask(0o077), env=wx_environment())
    except subprocess.TimeoutExpired:
        raise BridgeError('cli_timeout', 'The local database command timed out.')
    if result.returncode:
        if 'no key for account' in result.stderr:
            raise BridgeError('database_key_missing', 'No database key is stored for this account. UI control has separate permissions.')
        if 'permission denied' in result.stderr.lower() or 'operation not permitted' in result.stderr.lower():
            raise BridgeError('filesystem_permission', 'The CLI cannot access the local database directory.')
        raise BridgeError('database_query_failed', 'Database query failed. Raw stderr is withheld to protect account and message data.')
    validate_database_output(result.stderr)
    return result


def run_wx(argv):
    result = run_wx_process(argv)
    try:
        envelope = json.loads(result.stdout)
    except ValueError:
        raise BridgeError('invalid_cli_response', 'The database CLI returned an unexpected response.')
    validate_database_output(result.stderr, envelope)
    return envelope


def database_root(alias=None):
    scoped = account_args(alias)
    directory_name = scoped[-1]
    run_wx_process(['decrypt', '--incremental']+scoped)
    root = Path(wx_environment()['WX_CLI_CACHE_ROOT'])/directory_name/'db_storage'
    if not root.is_dir():
        raise BridgeError('database_cache_missing', 'The refreshed account database cache is missing.')
    return root


def native(action, **kwargs):
    if not Path(NATIVE).is_file():
        raise BridgeError('native_bridge_missing', 'An optional Accessibility helper is not installed. Database reading does not need it.')
    try:
        result = subprocess.run([NATIVE], input=json.dumps({'action': action, **kwargs}),
                                capture_output=True, text=True, timeout=25)
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise BridgeError('native_timeout', 'Native control timed out.')
    except ValueError:
        raise BridgeError('native_response_invalid', 'The native bridge did not return valid JSON.')
    if not data.get('ok'):
        raise BridgeError(data.get('code', 'native_error'), data.get('message', 'Native control failed.'))
    return data


def watch_sessions(args):
    seconds = args.get('seconds', 10)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 30:
        raise BridgeError('invalid_argument', 'seconds must be an integer between 1 and 30.')
    # Preflight through a normal query so missing keys fail immediately and safely.
    run_wx(['sessions', '--limit', '1', '--format', 'json', '--no-server'] + account_args(args.get('account')))
    process = subprocess.Popen([WX, 'watch', '--format', 'json', '--poll', '--poll-ms', '500']
                               + account_args(args.get('account')), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               env=wx_environment(),
                               preexec_fn=lambda: os.umask(0o077))
    try:
        output, diagnostics = process.communicate(timeout=seconds)
        if process.returncode:
            raise BridgeError('watch_failed', 'Session monitor failed; private stderr withheld.')
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            output, diagnostics = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            output, diagnostics = process.communicate()
    validate_database_output(diagnostics)
    events = []
    for line in output.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            raise BridgeError('invalid_watch_response', 'Session monitor returned an unexpected response.')
    return {'backend': 'wx_cli_database_session_monitor', 'events': events,
            'completeMessageStream': False, 'meaning': 'Session changes and latest previews; intermediate messages may be coalesced.'}


def text(value, field, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\0' in value:
        raise BridgeError('invalid_argument', field + ' is missing or exceeds the allowed length.')
    return value


def limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise BridgeError('invalid_argument', 'limit must be an integer between 1 and 100.')
    return value


def integer_arg(value, field, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63-1:
        raise BridgeError('invalid_argument', field+' must be a nonnegative integer.')
    return value


def paged_query(name, args):
    count = limit(args.get('limit', 20))
    offset = integer_arg(args.get('offset', 0), 'offset')
    order = args.get('order', 'desc')
    if order not in ('asc', 'desc'):
        raise BridgeError('invalid_argument', 'order must be asc or desc.')
    if name == 'wechat_sessions':
        argv = ['sessions', '--order', order]
    elif name == 'wechat_contacts':
        argv = ['contacts']
        if args.get('search') is not None:
            argv += ['--search', text(args['search'], 'search')]
    elif name == 'wechat_search':
        argv = ['search', text(args.get('keyword'), 'keyword')]
        mode=args.get('mode','native')
        if mode not in ('native','scan'):
            raise BridgeError('invalid_argument','mode must be native or scan.')
        if mode=='scan':argv.append('--scan')
    else:
        argv = ['query', text(args.get('contact'), 'contact'), '--order', order]
        for field, option in (('since','--since'),('until','--until')):
            if args.get(field) is not None:
                argv += [option, str(integer_arg(args[field],field))]
        if args.get('since') is not None and args.get('until') is not None and args['since']>args['until']:
            raise BridgeError('invalid_argument', 'since must be at or before until.')
        if args.get('type') is not None:
            argv += ['--type', text(args['type'],'type',30)]
        anchors = [(k,o) for k,o in (('aroundSortSeq','--around-sort-seq'),('aroundServerId','--around-server-id')) if args.get(k) is not None]
        if len(anchors)>1 or anchors and offset:
            raise BridgeError('invalid_argument','Use one context anchor with offset zero.')
        if anchors and (args.get('since') is not None or args.get('until') is not None):
            raise BridgeError('invalid_argument','Context anchors cannot be combined with a time range.')
        for k,o in anchors:
            argv += [o,str(integer_arg(args[k],k))]
        if args.get('context') is not None:
            if not anchors:
                raise BridgeError('invalid_argument','context requires an around anchor.')
            argv += ['--context',str(limit(args['context']))]
    if name != 'wechat_search':
        if type(args.get('showHidden',False)) is not bool:
            raise BridgeError('invalid_argument','showHidden must be a boolean.')
        if args.get('showHidden'):
            argv.append('--show-hidden')
    result = run_wx(argv+['--limit',str(count),'--offset',str(offset),'--format','json','--no-server']+account_args(args.get('account')))
    paging = result.get('paging',{})
    if isinstance(paging,dict):
        returned=paging.get('returned',len(result.get('items',[])))
        paging['nextOffset']=offset+returned if paging.get('has_more') else None
    return result


def list_files(args):
    scoped = account_args(args.get('account'))
    root = next(p for p in accounts().values() if p.name==scoped[-1]).resolve()
    relative = args.get('relativePath','.')
    if not isinstance(relative,str) or '\0' in relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise BridgeError('attachment_scope','Use a relative directory inside this account.')
    path = (root/relative).resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise BridgeError('attachment_scope','Only local directories inside this account are accessible.')
    count=limit(args.get('limit',50));offset=integer_arg(args.get('offset',0),'offset')
    entries=sorted((p for p in path.iterdir() if not p.is_symlink()),key=lambda p:p.name)
    items=[{'relativePath':str(p.relative_to(root)),'path':str(p),'name':p.name,
            'directory':p.is_dir(),'bytes':p.stat().st_size if p.is_file() else None} for p in entries[offset:offset+count]]
    return {'items':items,'paging':{'offset':offset,'returned':len(items),'total':len(entries),
                                   'has_more':offset+len(items)<len(entries),
                                   'nextOffset':offset+len(items) if offset+len(items)<len(entries) else None}}


def read_file(args):
    import base64
    requested = Path(text(args.get('path'),'path',4096)).expanduser().resolve(strict=True)
    scoped = account_args(args.get('account'))
    source = next(p for p in accounts().values() if p.name==scoped[-1]).resolve()
    media_root = private_state()/'media'
    if not (requested.is_relative_to(source) or requested.is_relative_to(media_root.resolve())) or not requested.is_file():
        raise BridgeError('attachment_scope','Only downloaded account files or exported media are accessible.')
    offset=integer_arg(args.get('offset',0),'offset');length=args.get('length',65536)
    if type(length) is not int or not 1<=length<=65536:
        raise BridgeError('invalid_argument','length must be an integer between 1 and 65536.')
    with requested.open('rb') as file:
        size=os.fstat(file.fileno()).st_size;file.seek(offset);chunk=file.read(length)
    return {'path':str(requested),'bytes':size,'offset':offset,'returnedBytes':len(chunk),
            'base64':base64.b64encode(chunk).decode('ascii'),
            'nextOffset':offset+len(chunk) if offset+len(chunk)<size else None,
            'complete':offset+len(chunk)>=size}


def service_read(name,args):
    from backends.receive import local_service as service
    scoped=account_args(args.get('account'))
    if name=='wechat_timeline':
        since=integer_arg(args.get('since'),'since');until=integer_arg(args.get('until'),'until')
        if since>until:raise BridgeError('invalid_argument','since must be at or before until.')
        order=args.get('order','desc')
        if order not in ('asc','desc'):raise BridgeError('invalid_argument','order must be asc or desc.')
        if type(args.get('showHidden',False)) is not bool:raise BridgeError('invalid_argument','showHidden must be a boolean.')
        params={'since':since,'until':until,'limit':limit(args.get('limit',20)),
                'offset':integer_arg(args.get('offset',0),'offset'),'order':order,
                'show_hidden':'true' if args.get('showHidden') else 'false'}
        if args.get('type') is not None:params['type']=text(args['type'],'type',30)
    elif name=='wechat_read_media':
        fmt=args.get('format','ogg')
        if fmt not in ('ogg','mp3'):raise BridgeError('invalid_argument','format must be ogg or mp3.')
        params={'talker':text(args.get('contact'),'contact'),
                'server_id':integer_arg(args.get('serverId'),'serverId'),'format':fmt}
    else:
        seconds=args.get('seconds',10)
        if type(seconds) is not int or not 1<=seconds<=30:
            raise BridgeError('invalid_argument','seconds must be between 1 and 30.')
    database_root(args.get('account'))
    try:
        config=service.ensure(private_state(),scoped,run_wx_process,write_private)
        if name=='wechat_read_media':return service.media(config,params,private_state())
        if name=='wechat_read_events':return service.events(config,seconds)
        result=service.query(config,'timeline',params)
        validate_database_output('',result)
        paging=result.get('paging',{})
        paging['nextOffset']=paging.get('offset',0)+paging.get('returned',0) if paging.get('has_more') else None
        return result
    except service.ServiceError as error:
        if name == 'wechat_read_media' and error.code == 'local_service_http_415':
            try:
                _, parent = merged_parent({'contact': args.get('contact'), 'account': args.get('account'), 'parentServerId': args.get('serverId')})
                if 'MergedMessages' in parent.get('content', {}):
                    raise BridgeError('merged_attachment_selector_required', 'The outer message is a merged record. Use wechat_list_merged_attachments then wechat_read_merged_attachment, or supply mergedItemPath to wechat_read_media.')
            except BridgeError as lookup_error:
                if lookup_error.code == 'merged_attachment_selector_required':raise
        raise BridgeError(error.code,'Local WeChat service read failed; private details withheld.')


def attachment(path):
    requested = Path(text(path, 'path', 4096)).expanduser().resolve()
    owned = next((p for p in accounts().values() if requested.is_relative_to(p.resolve())), None)
    if owned is None or not requested.is_file():
        raise BridgeError('attachment_scope', 'Only existing attachments within a detected WeChat account are accessible.')
    if requested.suffix.lower() == '.dat':
        destination = private_state() / ('attachment_' + secrets.token_hex(8))
        destination.mkdir(mode=0o700)
        try:
            result = subprocess.run([WX, 'decode-image', str(requested), '-d', str(owned), '-o', str(destination/'decoded.png')],
                                    capture_output=True, text=True, timeout=60,
                                    env=wx_environment(),preexec_fn=lambda:os.umask(0o077))
        except subprocess.TimeoutExpired:
            raise BridgeError('attachment_decode_timeout','Local image decoding timed out.')
        decoded = [p for p in destination.iterdir() if p.is_file()]
        if result.returncode or not decoded:
            raise BridgeError('attachment_decode_failed', 'This image could not be decoded. Raw output is withheld.')
        for p in decoded:
            os.chmod(p, 0o600)
        return {'files': [{'path': str(p), 'bytes': p.stat().st_size} for p in decoded]}
    return {'files': [{'path': str(requested), 'bytes': requested.stat().st_size}], 'decoded': False}


def draft_file(draft_id):
    if not isinstance(draft_id, str) or len(draft_id) != 32 or any(c not in '0123456789abcdef' for c in draft_id):
        raise BridgeError('invalid_draft', 'Invalid draft identifier.')
    return private_state() / (draft_id + '.json')


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(data, output, ensure_ascii=False)


def sender_backend():
    from backends.send.chatter import ChatterBackend, SendError
    path = ROOT / '.state/native-sender.json'
    if not path.is_file():
        raise BridgeError('native_sender_unconfigured', 'No native sender is configured or verified on this Mac.')
    if path.stat().st_mode & 0o077:
        raise BridgeError('unsafe_sender_config', 'Native sender configuration must have permissions 0600.')
    try:
        config = json.loads(path.read_text())
        return ChatterBackend(config['endpoint'], config['token'], config['version'],
                              config.get('verified_build_sha256'))
    except (ValueError, KeyError, SendError):
        raise BridgeError('invalid_sender_config', 'Invalid local native sender configuration; private details withheld.')


def merged_parent(args):
    contact = text(args.get('contact'), 'contact')
    server_id, sort_seq = args.get('parentServerId'), args.get('parentSortSeq')
    if (server_id is None) == (sort_seq is None):
        raise BridgeError('merged_parent_selector_required', 'Choose exactly one parentServerId or parentSortSeq; inner IDs are not parent message IDs.')
    query = {'contact': contact, 'context': 1, 'limit': 3}
    if args.get('account') is not None:query['account'] = args['account']
    if server_id is not None:
        if isinstance(server_id, str) and re.fullmatch('[0-9]{1,19}', server_id):server_id = int(server_id)
        server_id = integer_arg(server_id, 'parentServerId', 1)
        query['aroundServerId'] = server_id
    else:
        sort_seq = integer_arg(sort_seq, 'parentSortSeq', 1)
        query['aroundSortSeq'] = sort_seq
    data = paged_query('wechat_read_messages', query)
    matches = [m for m in data.get('items', []) if m.get('server_id') == server_id] if server_id is not None else [m for m in data.get('items', []) if m.get('sort_seq') == sort_seq]
    if len(matches) != 1:
        raise BridgeError('merged_parent_not_found_or_ambiguous', 'The exact parent message was not uniquely found in this conversation.')
    return contact, matches[0]


def merged_attachment(name, args):
    from backends.receive import merged_attachments as merged
    try:
        contact, message = merged_parent(args)
        items = merged.file_items(message)
        if name == 'wechat_list_merged_attachments':
            names = args.get('fileNames')
            if names is not None:
                if not isinstance(names, list) or not names or len(names) > 100 or any(not isinstance(n, str) for n in names):
                    raise BridgeError('invalid_argument', 'fileNames must be a nonempty array of exact file names.')
                items = [item for item in items if item['fileName'] in names]
            return {'parent': merged.parent_identity(message, contact), 'items': items, 'originalFilesObtained': False,
                    'backend': 'merged_record_xml_metadata', 'innerIdsAreParentConversationMessageIds': False}
        item = merged.select(items, args.get('itemPath'), args.get('dataId'))
        scoped = account_args(args.get('account'))
        account_root = next(p for p in accounts().values() if p.name == scoped[-1])
        media = private_state() / 'media'
        media.mkdir(mode=0o700, exist_ok=True)
        allow_download = args.get('download', True)
        if type(allow_download) is not bool:
            raise BridgeError('invalid_argument', 'download must be a boolean.')
        return merged.acquire(account_root, media, message, contact, item,
                              allow_download=allow_download,
                              force_download=name == 'wechat_download_merged_attachment')
    except merged.AttachmentError as error:
        raise BridgeError(error.code, 'Merged-record file lookup failed; CDN credentials and private details withheld.')


def call(name, args):
    if not isinstance(args, dict):
        raise BridgeError('invalid_argument', 'Arguments must be an object.')
    if READ_ONLY_MODE and name not in {t['name'] for t in TOOLS if t['annotations']['readOnlyHint']}:
        raise BridgeError('read_only_mode','This server exposes only WeChat reading tools.')
    if NATIVE_SEND_MODE and name not in {t['name'] for t in available_tools()}:
        raise BridgeError('native_send_mode', 'This server exposes database reads and configured native text tools.')
    if name in ('wechat_list_merged_attachments', 'wechat_read_merged_attachment', 'wechat_download_merged_attachment'):
        return merged_attachment(name, args)
    if name == 'wechat_read_media' and (args.get('mergedItemPath') is not None or args.get('mergedDataId') is not None):
        return merged_attachment('wechat_read_merged_attachment', {'contact': args.get('contact'), 'account': args.get('account'),
            'parentServerId': args.get('serverId'), 'itemPath': args.get('mergedItemPath'), 'dataId': args.get('mergedDataId')})
    if name in ('wechat_prepare_native_text', 'wechat_submit_native_text', 'wechat_native_text_job'):
        from backends.send.native_413 import agent_sender as sender
        try:
            if name == 'wechat_prepare_native_text':return sender.prepare(args.get('recipientId'), args.get('message'))
            if name == 'wechat_submit_native_text':return sender.submit(args.get('draftId'), args.get('userAuthorized'))
            return sender.job_status(args.get('jobId'))
        except sender.SenderError as error:
            raise BridgeError(error.code, 'Native text sender refused; check sender status. No automatic retry.')
    if name == 'wechat_list_files':
        return list_files(args)
    if name == 'wechat_read_file':return read_file(args)
    if name in ('wechat_timeline','wechat_read_media','wechat_read_events'):
        return service_read(name,args)
    if name in ('wechat_databases','wechat_database_tables','wechat_read_table','wechat_read_blob','wechat_sql_query'):
        from backends.receive import data_access as data
        root=database_root(args.get('account'))
        try:
            if name=='wechat_databases':return data.manifest(root)
            database=text(args.get('database'),'database',512)
            if name=='wechat_database_tables':return data.schema(root,database)
            if name=='wechat_sql_query':return data.sql_query(root,database,args.get('sql'),args.get('params'),limit(args.get('limit',100)))
            table=text(args.get('table'),'table',256)
            if name=='wechat_read_table':return data.read_table(root,database,table,limit=limit(args.get('limit',50)),offset=integer_arg(args.get('offset',0),'offset'),where=args.get('where'))
            return data.read_blob(root,database,table,args.get('column'),args.get('selector'),offset=args.get('offset',0),length=args.get('length',65536))
        except data.DataError as error:
            raise BridgeError(error.code,'Local database read failed or was rejected; private details withheld.')
    if name == 'wechat_doctor':
        from backends.receive.preflight import inspect
        status = inspect()
        status['accessibilityHelperInstalled'] = Path(NATIVE).is_file()
        status['databaseAccounts'] = len(accounts())
        status['wxCliExists'] = Path(WX).is_file()
        if status['wxCliExists']:
            paths = run_wx(['paths', '--json'])
            status['databaseKeyFileExists'] = Path(paths['keys_file']).is_file()
        else:
            status['databaseKeyFileExists'] = None
        return status
    if name == 'wechat_key_preflight':
        from backends.receive.preflight import inspect
        result = inspect()
        paths = run_wx(['paths', '--json'])
        result['keyFilePresent'] = Path(paths['keys_file']).is_file()
        return result
    if name == 'wechat_accounts':
        return {'accounts': [{'alias': alias, 'messageDatabaseExists': (p / 'db_storage/message/message_0.db').is_file()}
                             for alias, p in accounts().items()]}
    if name == 'wechat_capabilities':
        paths = run_wx(['paths', '--json'])
        key_path = Path(paths['keys_file'])
        verified = {}
        proof = STATE / 'readiness.json'
        if proof.is_file() and key_path.is_file():
            saved = json.loads(proof.read_text())
            if saved.get('keyStoreSha256') == hashlib.sha256(key_path.read_bytes()).hexdigest():
                verified = saved
        return {'platform': 'macOS', 'accountCount': len(accounts()),
                'databaseKeyFileExists': key_path.is_file(),
                'history': {'backend': 'wx_cli_database', 'requiresValidatedDatabaseKey': True, 'verifiedReadable': verified.get('history', False)},
                'localData':verified.get('localData',{'verified':False}),
                'mergedAttachments': {'backend':'merged_record_local_or_cdn_original','supported':True,'verifiesSizeAndMd5':True,
                                      'automaticRemoteDownload':True,'requiresClientDownloadWhenMissing':False,
                                      'requiresClientCdnRouteCache':True,'requiresGui':False,
                                      'forceDownloadTool':'wechat_download_merged_attachment'},
                'agentAccess':{'readOnlyMode':READ_ONLY_MODE,'mcp':'stdio','cli':True},
                'incrementalReceive': {'backend': 'wx_cli_database', 'newRowVerified': verified.get('newRow', False), 'completeMessageStream': False},
                'sessionWatch': {'backend': 'wx_cli_database_session_monitor', 'completeMessageStream': False},
                'personalNativeSend': native_text_status(),
                'visibleRead': {'backend': 'macos_accessibility_or_gui_ocr', 'completeHistory': False},
                'note': 'Installed interfaces and a key file do not prove successful decryption or delivery.'}
    if name == 'wechat_watch_sessions':
        return watch_sessions(args)
    if name in ('wechat_receive_start', 'wechat_receive_pull'):
        from backends.receive.incremental import initialize, pull, ReceiveError
        contact = text(args.get('contact'), 'contact')
        account = args.get('account')
        if not account:
            available = accounts()
            if len(available) != 1:
                raise BridgeError('account_selection_required', 'Select one local account alias explicitly.')
            account = next(iter(available))
        scoped_args = account_args(account)
        try:
            if name == 'wechat_receive_start':
                result = initialize(run_wx, contact, scoped_args)
            else:
                cursor = text(args.get('cursor'), 'cursor', 32)
                if len(cursor) != 32 or any(c not in '0123456789abcdef' for c in cursor):
                    raise BridgeError('invalid_cursor', 'Invalid receive cursor.')
                path = private_state() / ('receive-' + cursor + '.json')
                if not path.is_file():
                    raise BridgeError('invalid_cursor', 'Receive cursor not found.')
                saved = json.loads(path.read_text())
                if saved['contact'] != contact or saved['account'] != account:
                    raise BridgeError('cursor_scope_mismatch', 'Receive cursor belongs to a different conversation or account.')
                result = pull(run_wx, contact, saved['sortSeq'], scoped_args, limit(args.get('limit', 50)))
        except ReceiveError:
            raise BridgeError('receive_query_invalid', 'Incremental receive cannot safely advance its cursor; use a scoped history query.')
        token = secrets.token_hex(16)
        write_private(private_state() / ('receive-' + token + '.json'),
                      {'contact': contact, 'account': account, 'sortSeq': result['sortSeq']})
        result['cursor'] = token
        return result
    if name == 'wechat_native_sender_status':
        if NATIVE_SEND_MODE or (STATE / 'native-agent-sender.json').is_file() or not (STATE / 'native-sender.json').is_file():return native_text_status()
        from backends.send.chatter import SendError
        try:
            return sender_backend().capabilities(probe=args.get('probe', False) is True)
        except SendError as error:
            raise BridgeError(error.code, 'Native sender probe failed; private details withheld.')
    if name == 'wechat_prepare_native_message':
        backend = sender_backend()
        if not backend.verified:
            raise BridgeError('version_build_not_verified', 'The native backend build is not attested. No message was sent.')
        recipient = text(args.get('recipientId'), 'recipientId', 128)
        message = text(args.get('message'), 'message', 10000)
        draft_id = secrets.token_hex(16)
        write_private(draft_file(draft_id), {'contact': recipient, 'message': message, 'backend': 'native_chatter',
                                           'configDigest': hashlib.sha256((ROOT / '.state/native-sender.json').read_bytes()).hexdigest(),
                                           'expires': time.time() + 600, 'approval': None, 'used': False})
        return {'draftId': draft_id, 'sent': False, 'approvalRequired': True,
                'approvalCommand': 'wechat-bridge approve ' + draft_id}
    if name in ('wechat_sessions', 'wechat_contacts', 'wechat_read_messages', 'wechat_search'):
        return paged_query(name,args)
    if name == 'wechat_open_chat':
        return native('open_chat', contact=text(args.get('contact'), 'contact'))
    if name == 'wechat_read_visible':
        return native('read_visible', contact=text(args.get('contact'), 'contact'))
    if name == 'wechat_get_attachment':
        return attachment(args.get('path'))
    if name == 'wechat_prepare_message':
        contact = text(args.get('contact'), 'contact')
        message = text(args.get('message'), 'message', 10000)
        result = native('prepare_message', contact=contact, message=message)
        draft_id = secrets.token_hex(16)
        write_private(draft_file(draft_id), {'contact': contact, 'message': message, 'expires': time.time() + 600,
                                           'approval': None, 'used': False})
        return {'draftId': draft_id, 'sent': False, 'prepared': result.get('prepared', False),
                'approvalRequired': True, 'approvalCommand': 'wechat-bridge approve ' + draft_id}
    if name == 'wechat_send_message':
        path = draft_file(args.get('draftId'))
        if not path.is_file():
            raise BridgeError('invalid_draft', 'Draft was not found.')
        draft = json.loads(path.read_text())
        if draft['used'] or draft['expires'] < time.time():
            raise BridgeError('draft_expired', 'Draft is expired or already used.')
        token = args.get('approvalToken')
        if not isinstance(token, str) or not draft['approval'] or not secrets.compare_digest(token, draft['approval']):
            raise BridgeError('send_requires_approval', 'A matching local approval token is required. No message was sent.')
        # Consume approval before side effect. An uncertain result requires a new review;
        # never auto-retry a message which may already have been sent.
        draft['used'] = True
        write_private(path, draft)
        if draft.get('backend') == 'native_chatter':
            from backends.send.chatter import SendError
            config_path = ROOT / '.state/native-sender.json'
            if not config_path.is_file() or hashlib.sha256(config_path.read_bytes()).hexdigest() != draft.get('configDigest'):
                raise BridgeError('sender_config_changed', 'Sender configuration changed after review. Approval consumed; create a new ticket.')
            try:
                return sender_backend().send(draft['contact'], text=draft['message'])
            except SendError as error:
                raise BridgeError(error.code, 'Native send failed or is uncertain. Approval consumed; do not retry automatically.')
        return native('send_message', contact=draft['contact'], message=draft['message'])
    raise BridgeError('unknown_tool', 'Unknown WeChat bridge tool.')


def tool(name, description, properties=None, required=None, read_only=True):
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False},
            'annotations': {'readOnlyHint': read_only, 'openWorldHint': name in ('wechat_send_message', 'wechat_submit_native_text', 'wechat_read_media', 'wechat_read_merged_attachment', 'wechat_download_merged_attachment')}}


def native_text_status():
    from backends.send.native_413 import agent_sender
    return agent_sender.status()


CONTACT = {'type': 'string', 'minLength': 1, 'maxLength': 200}
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20}
OFFSET = {'type':'integer','minimum':0,'default':0}
PAGE = {'account':CONTACT,'limit':LIMIT,'offset':OFFSET}
ORDER = {'type':'string','enum':['asc','desc'],'default':'desc'}
SHOW_HIDDEN = {'type':'boolean','default':False}
MERGED_PATH = {'type':'array','minItems':1,'maxItems':9,'items':{'type':'integer','minimum':0,'maximum':2047}}
MERGED_PARENT = {'account':CONTACT,'contact':CONTACT,'parentSortSeq':{'type':'integer','minimum':1,'maximum':2**63-1},
    'parentServerId':{'oneOf':[{'type':'string','pattern':'^[0-9]{1,19}$'},{'type':'integer','minimum':1,'maximum':2**63-1}]}}
TOOLS = [
    tool('wechat_doctor', 'Check local WeChat, Accessibility and database prerequisites; does not grant permissions.'),
    tool('wechat_key_preflight', 'Read-only key-path and system preflight. Never signs, attaches, reads key contents or changes SIP.'),
    tool('wechat_accounts', 'List local account aliases without exposing account IDs.'),
    tool('wechat_capabilities', 'Report backend types and honest readiness boundaries without reading messages.'),
    tool('wechat_sessions', 'Enumerate local conversations with pagination. Follow paging.nextOffset to read every page.', {**PAGE,'order':ORDER,'showHidden':SHOW_HIDDEN}),
    tool('wechat_contacts', 'Read/search all local contacts and groups, with pagination and labels/profile fields.', {**PAGE,'search':CONTACT,'showHidden':SHOW_HIDDEN}),
    tool('wechat_read_messages', 'Read complete paginated history, including structured non-text messages. Supports time/type filters and context anchors. For all history use asc and follow paging.nextOffset.', {**PAGE,'contact':CONTACT,'order':ORDER,'showHidden':SHOW_HIDDEN,'since':OFFSET,'until':OFFSET,'type':CONTACT,'aroundSortSeq':OFFSET,'aroundServerId':OFFSET,'context':LIMIT}, ['contact']),
    tool('wechat_search', 'Search across local conversations. Native uses the WeChat word index; scan checks decoded bodies for a literal substring, including numbers omitted by the index. Follow nextOffset for all hits.', {**PAGE,'keyword':CONTACT,'mode':{'type':'string','enum':['native','scan'],'default':'native'}}, ['keyword']),
    tool('wechat_timeline','Read a time-bounded timeline across all local conversations using the original loopback API. Follow nextOffset for every page.',{**PAGE,'since':OFFSET,'until':OFFSET,'order':ORDER,'showHidden':SHOW_HIDDEN,'type':CONTACT},['since','until']),
    tool('wechat_read_media','Read original local media by exact conversation and outer message server ID. Merged-record files require mergedItemPath or mergedDataId; inner original IDs must not be used as independent messages in this group.',{'account':CONTACT,'contact':CONTACT,'serverId':OFFSET,'format':{'type':'string','enum':['ogg','mp3'],'default':'ogg'},'mergedItemPath':MERGED_PATH,'mergedDataId':CONTACT},['contact','serverId']),
    tool('wechat_list_merged_attachments','List safe file metadata within one exact merged record. Choose one parentSortSeq or parentServerId (use decimal strings for 64-bit IDs), optionally filter exact fileNames. No CDN keys/URLs; no claim that file bytes were obtained.',{**MERGED_PARENT,'fileNames':{'type':'array','items':CONTACT,'minItems':1,'maxItems':100}},['contact']),
    tool('wechat_read_merged_attachment','Get an original merged-record file: locate locally, otherwise download and AES-decrypt via the client CDN cache, then verify exact size and MD5. Choose one parentSortSeq/parentServerId and one itemPath/dataId. Set download=false for local-only reads. No GUI clicks or message sends.',{**MERGED_PARENT,'itemPath':MERGED_PATH,'dataId':CONTACT,'download':{'type':'boolean','default':True}},['contact']),
    tool('wechat_download_merged_attachment','Always download a fresh original merged-record file from WeChat CDN, AES-decrypt and verify exact size and MD5. Bypasses all local file copies. Choose one parentSortSeq/parentServerId and one itemPath/dataId. Requires an existing client CDN cache, no GUI or message sends.',{**MERGED_PARENT,'itemPath':MERGED_PATH,'dataId':CONTACT},['contact']),
    tool('wechat_read_events','Read a bounded interval of original local SSE events, including message batches. Reconnect gaps require history/timeline recovery; no gap-free claim.',{'account':CONTACT,'seconds':{'type':'integer','minimum':1,'maximum':30,'default':10}}),
    tool('wechat_databases','List every refreshed local WeChat database, including favorites, SNS, media and business-message stores.',{'account':CONTACT}),
    tool('wechat_database_tables','Inspect tables, columns, keys and schema of a local WeChat database.',{'account':CONTACT,'database':CONTACT},['database']),
    tool('wechat_read_table','Read any local WeChat table with pagination and parameterized equality filters. Binary fields include locators for complete chunked reads.',{**PAGE,'database':CONTACT,'table':CONTACT,'where':{'type':'object'}},['database','table']),
    tool('wechat_read_blob','Read a complete binary/text field in chunks using a locator from read_table. Follow nextOffset until complete.',{'account':CONTACT,'database':CONTACT,'table':CONTACT,'column':CONTACT,'selector':{'type':'object'},'offset':OFFSET,'length':{'type':'integer','minimum':1,'maximum':65536,'default':65536}},['database','table','column','selector']),
    tool('wechat_sql_query','Run one read-only SELECT/CTE on a local WeChat database. Mutations, ATTACH, PRAGMA changes and file/extension functions are rejected. Results capped per call; paginate explicitly.',{'account':CONTACT,'database':CONTACT,'sql':{'type':'string','maxLength':20000},'params':{'oneOf':[{'type':'array'},{'type':'object'}]},'limit':LIMIT},['database','sql']),
    tool('wechat_list_files','Browse all downloaded local WeChat account files and media folders with pagination. Use returned paths with get_attachment.',{**PAGE,'relativePath':CONTACT}),
    tool('wechat_read_file','Read any downloaded account file or exported media in chunks. Follow nextOffset to read all bytes.',{'account':CONTACT,'path':{'type':'string'},'offset':OFFSET,'length':{'type':'integer','minimum':1,'maximum':65536,'default':65536}},['path']),
    tool('wechat_watch_sessions', 'Watch database session changes for a bounded interval. Requires a key; latest previews are not a complete new-message stream.', {'account': CONTACT, 'seconds': {'type': 'integer', 'minimum': 1, 'maximum': 30, 'default': 10}}),
    tool('wechat_receive_start', 'Create a database message cursor from now for one conversation. Requires a validated key; does not read old messages as new.', {'contact': CONTACT, 'account': CONTACT}, ['contact']),
    tool('wechat_receive_pull', 'Pull actual database message rows after a bound cursor. Save returned cursor only after consuming the batch; not guaranteed for late lower-sequence inserts.', {'contact': CONTACT, 'account': CONTACT, 'cursor': {'type': 'string'}, 'limit': LIMIT}, ['contact', 'cursor']),
    tool('wechat_native_sender_status', 'Check native sender configuration/build/login prerequisites without sending or launching WeChat. Optional probe only applies to a configured HTTP adapter.', {'probe': {'type': 'boolean', 'default': False}}),
    tool('wechat_prepare_native_text', 'Prepare exact File Helper text as private JSON, without launching WeChat or sending.', {'recipientId': {'type': 'string', 'enum': ['filehelper']}, 'message': {'type': 'string', 'minLength': 1, 'maxLength': 16384}}, ['recipientId', 'message'], False),
    tool('wechat_submit_native_text', 'Submit a prepared text once. userAuthorized attests an explicit human request for the exact recipient/text. Returns a job, not delivery. Requires compatible native setup, original WeChat quit, and temporary-client login. Never replay uncertainty.', {'draftId': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}, 'userAuthorized': {'type': 'boolean', 'const': True}}, ['draftId', 'userAuthorized'], False),
    tool('wechat_native_text_job', 'Read native job progress/result without retrying. Login, queued, acknowledgement and recipient receipt are separate states.', {'jobId': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}}, ['jobId']),
    tool('wechat_prepare_native_message', 'Prepare an approval ticket for an explicitly configured native backend. Does not touch the UI or send; requires an attested build and exact recipient ID.', {'recipientId': CONTACT, 'message': {'type': 'string', 'maxLength': 10000}}, ['recipientId', 'message'], False),
    tool('wechat_open_chat', 'Open an exact conversation through macOS Accessibility; never sends.', {'contact': CONTACT}, ['contact'], False),
    tool('wechat_read_visible', 'Read visible text from an exact conversation, changing the active UI. Not complete history.', {'contact': CONTACT}, ['contact'],False),
    tool('wechat_get_attachment', 'Resolve an existing local WeChat attachment and decode supported .dat images.', {'path': {'type': 'string'}}, ['path']),
    tool('wechat_prepare_message', 'Put text in an exact conversation as an unsent draft. Requires local review before sending.', {'contact': CONTACT, 'message': {'type': 'string', 'maxLength': 10000}}, ['contact', 'message'], False),
    tool('wechat_send_message', 'Send an exact previously reviewed draft once. Never use without specific human recipient/text authorization.', {'draftId': {'type': 'string'}, 'approvalToken': {'type': 'string'}}, ['draftId', 'approvalToken'], False),
]


def available_tools():
    return [t for t in TOOLS if (not READ_ONLY_MODE or t['annotations']['readOnlyHint']) and
            (not NATIVE_SEND_MODE or t['annotations']['readOnlyHint'] or t['name'] in
             ('wechat_prepare_native_text', 'wechat_submit_native_text'))]


def handle_rpc(request):
    request_id = request.get('id')
    method = request.get('method')
    if request_id is None:
        return None
    if method == 'initialize':
        wanted = request.get('params', {}).get('protocolVersion', '2024-11-05')
        return {'jsonrpc': '2.0', 'id': request_id, 'result': {'protocolVersion': wanted if wanted in ['2024-11-05', '2025-03-26', '2025-06-18'] else '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'mac-wechat-local-bridge', 'version': '0.1.0'}}}
    if method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': available_tools()}
    elif method == 'tools/call':
        params = request.get('params', {})
        try:
            data = call(params.get('name'), params.get('arguments', {}))
            result = {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}], 'isError': False}
        except BridgeError as error:
            result = {'content': [{'type': 'text', 'text': json.dumps({'error': error.code, 'message': str(error)})}], 'isError': True}
        except Exception:
            result = {'content': [{'type': 'text', 'text': '{"error":"internal_error","message":"Local bridge failed; private details withheld."}'}], 'isError': True}
    else:
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def main():
    global READ_ONLY_MODE, NATIVE_SEND_MODE
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--read-only',action='store_true')
    mode.add_argument('--native-send',action='store_true')
    parser.add_argument('command', choices=['mcp', 'call', 'approve'])
    parser.add_argument('name', nargs='?')
    parser.add_argument('arguments', nargs='?', default='{}')
    args = parser.parse_args()
    READ_ONLY_MODE = READ_ONLY_MODE or args.read_only
    NATIVE_SEND_MODE = args.native_send
    if READ_ONLY_MODE and NATIVE_SEND_MODE:parser.error('Native sends cannot be enabled in a read-only environment.')
    if READ_ONLY_MODE and args.command=='approve':
        parser.error('Read-only mode cannot approve sends.')
    if args.command == 'mcp':
        for line in sys.stdin:
            try:
                response = handle_rpc(json.loads(line))
            except Exception:
                response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
        return
    try:
        if args.command == 'approve':
            if not sys.stdin.isatty():
                raise BridgeError('interactive_review_required', 'Approve must run in a local interactive terminal.')
            path = draft_file(args.name)
            draft = json.loads(path.read_text())
            if draft['used'] or draft['expires'] < time.time():
                raise BridgeError('draft_expired', 'Draft is expired or already used.')
            print('Recipient:', draft['contact'])
            print('Message:', draft['message'])
            if input('Type SEND to authorize this exact draft: ') != 'SEND':
                raise BridgeError('approval_cancelled', 'No approval issued.')
            draft['approval'] = secrets.token_urlsafe(32)
            write_private(path, draft)
            data = {'draftId': args.name, 'approvalToken': draft['approval']}
        else:
            data = call(args.name, json.loads(args.arguments))
        print(json.dumps(data, ensure_ascii=False))
    except BridgeError as error:
        print(json.dumps({'error': error.code, 'message': str(error)}))
        sys.exit(1)
    except Exception:
        print('{"error":"internal_error","message":"Local bridge failed; private details withheld."}')
        sys.exit(1)


if __name__ == '__main__':
    main()

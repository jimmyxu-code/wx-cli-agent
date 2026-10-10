"""Read-only access to every refreshed local WeChat database, not only chats.

The caller supplies a current, validated decrypted snapshot root. Never opens
the original encrypted databases with SQLite, or follows paths out of this root.
"""
import base64
from contextlib import closing
import sqlite3
import time
from pathlib import Path


class DataError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def integer(value, minimum=0, maximum=2**63-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise DataError('invalid_argument')
    return value


def identifier(value):
    if not isinstance(value, str) or not value or '\0' in value or len(value) > 256:
        raise DataError('invalid_argument')
    return '"'+value.replace('"', '""')+'"'


def database_path(root, relative):
    root = Path(root).resolve(strict=True)
    if (not isinstance(relative, str) or '\0' in relative or '\\' in relative
            or len(relative) > 512):
        raise DataError('database_scope')
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or rel.suffix != '.db':
        raise DataError('database_scope')
    path = root/rel
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise DataError('database_not_found')
    if resolved != path or not resolved.is_relative_to(root) or not resolved.is_file():
        raise DataError('database_scope')
    return resolved


def connect(path):
    try:
        # The Rust cache has already merged committed encrypted WAL frames.
        # A plaintext snapshot must not reopen copied source WAL/SHM files.
        db = sqlite3.connect(Path(path).as_uri()+'?mode=ro&immutable=1', uri=True)
        db.execute('PRAGMA query_only=ON')
        db.row_factory = sqlite3.Row
        db.text_factory = lambda data: data.decode('utf-8') if _valid_utf8(data) else data
        return db
    except sqlite3.Error:
        raise DataError('database_unreadable')


def _valid_utf8(data):
    try:
        data.decode('utf-8')
        return True
    except UnicodeError:
        return False


def tables(db):
    return [dict(row) for row in db.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]


def columns(db, name):
    if name not in {row['name'] for row in tables(db)}:
        raise DataError('unknown_table')
    return [dict(row) for row in db.execute('PRAGMA table_info('+identifier(name)+')')]


def json_value(value, locator=None):
    if isinstance(value, bytes):
        preview = value[:256]
        return {'kind': 'blob', 'bytes': len(value),
                'base64': base64.b64encode(preview).decode('ascii'),
                'previewBytes': len(preview), 'truncated': len(value)>len(preview),
                'readWith': locator}
    return value


def selector_value(value):
    if isinstance(value, bytes):
        return {'blobBase64': base64.b64encode(value).decode('ascii')}
    return value


def filter_value(value):
    if value is None or type(value) in (str, int, float):
        return value
    if isinstance(value, dict) and set(value)=={'blobBase64'} and isinstance(value['blobBase64'], str):
        try:
            return base64.b64decode(value['blobBase64'], validate=True)
        except ValueError:
            pass
    raise DataError('invalid_filter')


def manifest(root):
    root = Path(root).resolve(strict=True)
    items = []
    for path in sorted(root.rglob('*.db')):
        relative = str(path.relative_to(root))
        path = database_path(root, relative)
        try:
            with closing(connect(path)) as db:
                count = len(tables(db))
            items.append({'database': relative, 'bytes': path.stat().st_size,
                          'readable': True, 'tableCount': count})
        except DataError as error:
            items.append({'database': relative, 'readable': False, 'error': error.code})
    return {'databases': items, 'databaseCount': len(items),
            'scope': 'All databases present in this local WeChat account; cloud-only data is not downloaded.'}


def schema(root, relative):
    with closing(connect(database_path(root, relative))) as db:
        items = [{**row, 'columns': columns(db, row['name'])} for row in tables(db)]
    return {'database': relative, 'tables': items}


def read_table(root, relative, table, *, limit=50, offset=0, where=None):
    integer(limit, 1, 100)
    integer(offset)
    with closing(connect(database_path(root, relative))) as db:
        info = columns(db, table)
        names = {col['name'] for col in info}
        keys = [col['name'] for col in sorted(info, key=lambda c: c['pk']) if col['pk']]
        where = {} if where is None else where
        if not isinstance(where, dict) or any(k not in names for k in where):
            raise DataError('invalid_filter')
        clause = ' AND '.join(identifier(k)+' IS ?' for k in where) or '1'
        values = [filter_value(v) for v in where.values()]
        is_view = next(row['type']=='view' for row in tables(db) if row['name']==table)
        # Prefer actual primary keys. Unkeyed tables use SQLite's rowid.
        rowid = not keys and not is_view and '_rowid_' not in names
        select = ('_rowid_ AS "__bridge_rowid__",' if rowid else '')+'*'
        order = ','.join(identifier(k) for k in keys) if keys else ('_rowid_' if rowid else '')
        query = 'SELECT '+select+' FROM '+identifier(table)+' WHERE '+clause
        if order:
            query += ' ORDER BY '+order
        try:
            total = db.execute('SELECT count(*) FROM '+identifier(table)+' WHERE '+clause, values).fetchone()[0]
            rows = db.execute(query+' LIMIT ? OFFSET ?', values+[limit, offset]).fetchall()
        except sqlite3.Error:
            raise DataError('table_query_failed')
        items = []
        for row in rows:
            selector = {key: selector_value(row[key]) for key in keys} if keys else (
                {'_rowid_': row['__bridge_rowid__']} if rowid else None)
            item = {}
            for name in row.keys():
                if name == '__bridge_rowid__':
                    continue
                locator = {'database': relative, 'table': table, 'column': name,
                           'selector': selector, 'offset': 0} if selector else None
                # Unkeyed views must remain readable even without a blob locator.
                item[name] = json_value(row[name], locator)
                if isinstance(row[name], bytes) and selector is None:
                    item[name]['base64'] = base64.b64encode(row[name]).decode('ascii')
                    item[name]['previewBytes'] = len(row[name]);item[name]['truncated'] = False
            items.append({'selector': selector, 'values': item})
    returned = len(items)
    return {'database': relative, 'table': table, 'items': items,
            'paging': {'limit': limit, 'offset': offset, 'returned': returned, 'total': total,
                       'has_more': offset+returned < total,
                       'nextOffset': offset+returned if offset+returned < total else None}}


def read_blob(root, relative, table, column, selector, *, offset=0, length=65536):
    integer(offset);integer(length, 1, 65536)
    with closing(connect(database_path(root, relative))) as db:
        info = columns(db, table);names = {c['name'] for c in info}
        keys = {c['name'] for c in info if c['pk']}
        if column not in names or not isinstance(selector, dict) or not selector:
            raise DataError('invalid_blob_locator')
        if set(selector) != (keys or {'_rowid_'}) or not keys and '_rowid_' in names:
            raise DataError('invalid_blob_locator')
        values = [filter_value(v) for v in selector.values()]
        clause = ' AND '.join(identifier(k)+' IS ?' for k in selector)
        col = 'CAST('+identifier(column)+' AS BLOB)'
        try:
            rows = db.execute('SELECT length('+col+'),substr('+col+',?,?) FROM '+identifier(table)
                              +' WHERE '+clause+' LIMIT 2', [offset+1, length]+values).fetchall()
        except sqlite3.Error:
            raise DataError('blob_query_failed')
        if len(rows) != 1 or rows[0][0] is None:
            raise DataError('blob_not_found_or_ambiguous')
        size, data = rows[0]
        data = data or b''
    return {'bytes': size, 'offset': offset, 'returnedBytes': len(data),
            'base64': base64.b64encode(data).decode('ascii'),
            'nextOffset': offset+len(data) if offset+len(data)<size else None,
            'complete': offset+len(data)>=size}


def sql_query(root, relative, sql, params=None, limit=100):
    integer(limit, 1, 100)
    if not isinstance(sql, str) or not sql.strip() or len(sql)>20000:
        raise DataError('invalid_sql')
    if params is not None and (not isinstance(params, (list, dict)) or len(params)>100):
        raise DataError('invalid_sql_parameters')
    with closing(connect(database_path(root, relative))) as db:
        until = time.monotonic()+5
        db.set_progress_handler(lambda: int(time.monotonic()>until), 1000)
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
        if hasattr(sqlite3, 'SQLITE_RECURSIVE'):
            allowed.add(sqlite3.SQLITE_RECURSIVE)
        def authorize(action, arg1, arg2, _database, _source):
            if action not in allowed:
                return sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_FUNCTION and str(arg2).lower() in (
                    'load_extension','readfile','writefile','randomblob','zeroblob'):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        try:
            cursor = db.execute(sql, params or [])
            names = [x[0] for x in cursor.description or []]
            rows = cursor.fetchmany(limit+1)
        except (sqlite3.Error, sqlite3.Warning, ValueError, TypeError):
            raise DataError('read_only_sql_rejected_or_failed')
        values = [[json_value(row[index]) for index in range(len(names))] for row in rows[:limit]]
        items = [dict(zip(names, row)) for row in values]
    return {'database': relative, 'columns': names, 'items': items,
            'rows': values,
            'hasMore': len(rows)>limit,
            'note': 'For complete large binary values use read_table and read_blob; paginate SQL explicitly.'}

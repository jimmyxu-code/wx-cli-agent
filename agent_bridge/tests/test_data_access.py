import base64
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backends.receive import data_access as data


class ReadOnlyDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.path=self.root/'fixture.db';self.blob=bytes(range(256))*400
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, body BLOB, label TEXT)')
            db.executemany('INSERT INTO records VALUES(?,?,?)',[(i,self.blob,str(i)) for i in range(1,106)])
            db.execute('CREATE TABLE index_data(term BLOB PRIMARY KEY, payload BLOB) WITHOUT ROWID')
            db.execute('INSERT INTO index_data VALUES(?,?)',(b'\xff\x00',self.blob))

    def tearDown(self):self.temp.cleanup()

    def test_history_pagination_has_no_first_page_cutoff(self):
        first=data.read_table(self.root,'fixture.db','records',limit=100)
        second=data.read_table(self.root,'fixture.db','records',limit=100,offset=first['paging']['nextOffset'])
        self.assertEqual(first['paging']['total'],105)
        self.assertEqual([x['values']['id'] for x in first['items']+second['items']],list(range(1,106)))
        self.assertFalse(second['paging']['has_more'])

    def test_binary_primary_keys_and_full_blob_roundtrip(self):
        row=data.read_table(self.root,'fixture.db','index_data')['items'][0]
        json.dumps(row)
        locator=row['values']['payload']['readWith'];offset=0;chunks=[]
        while True:
            part=data.read_blob(self.root,locator['database'],locator['table'],locator['column'],locator['selector'],offset=offset,length=17000)
            chunks.append(base64.b64decode(part['base64']))
            if part['complete']:break
            offset=part['nextOffset']
        self.assertEqual(b''.join(chunks),self.blob)
        self.assertEqual(len(data.read_table(self.root,'fixture.db','index_data',where=row['selector'])['items']),1)

    def test_sql_and_cte_can_read_but_cannot_mutate_or_open_other_files(self):
        original=self.path.read_bytes()
        result=data.sql_query(self.root,'fixture.db','WITH a AS (SELECT id FROM records WHERE id>?) SELECT * FROM a',[100])
        self.assertEqual([x['id'] for x in result['items']],[101,102,103,104,105])
        for sql in ('DELETE FROM records','UPDATE records SET label="changed"','INSERT INTO records VALUES(999,NULL,NULL)',
                    'CREATE TABLE leaked(x)','ATTACH DATABASE ":memory:" AS other','PRAGMA query_only=OFF',
                    "SELECT load_extension('anything')",'SELECT * FROM records; DELETE FROM records'):
            with self.subTest(sql=sql),self.assertRaises(data.DataError):
                data.sql_query(self.root,'fixture.db',sql)
        self.assertEqual(self.path.read_bytes(),original)

    def test_duplicate_sql_columns_remain_available_in_rows(self):
        result=data.sql_query(self.root,'fixture.db','SELECT 1 AS a, 2 AS a')
        self.assertEqual(result['columns'],['a','a']);self.assertEqual(result['rows'],[[1,2]])

    def test_paths_and_sql_identifiers_cannot_escape_account_snapshot(self):
        (self.root/'link.db').symlink_to(self.path)
        for path in ('../fixture.db',str(self.path),'link.db','missing.db'):
            with self.subTest(path=path),self.assertRaises(data.DataError):data.schema(self.root,path)
        with self.assertRaises(data.DataError):data.read_table(self.root,'fixture.db','records;DELETE FROM records')
        self.assertEqual(data.read_table(self.root,'fixture.db','records',where={'label':'1 OR 1=1'})['items'],[])

    def test_sql_result_limit_marks_remaining_rows(self):
        result=data.sql_query(self.root,'fixture.db','SELECT id FROM records ORDER BY id',limit=100)
        self.assertEqual(len(result['items']),100);self.assertTrue(result['hasMore'])

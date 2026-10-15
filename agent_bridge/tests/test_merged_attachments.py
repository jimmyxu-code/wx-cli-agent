import hashlib
import html
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backends.receive import merged_attachments as merged

spec = importlib.util.spec_from_file_location('merged_bridge_test', ROOT / 'wechat_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class MergedAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.account = self.root / 'account'
        self.account.mkdir()
        self.payload = b'%PDF-1.7\nfixture original PDF bytes\n%%EOF\n'
        self.md5 = hashlib.md5(self.payload).hexdigest()
        self.file_xml = '<dataitem datatype="8" dataid="file-entry"><datatitle>guide.pdf</datatitle><datafmt>pdf</datafmt><datasize>'+str(len(self.payload))+'</datasize><fullmd5>'+self.md5+'</fullmd5><fromnewmsgid>9007199254741009</fromnewmsgid><cdndatakey>PRIVATE_CDN_KEY</cdndatakey><cdndataurl>PRIVATE_CDN_ID</cdndataurl></dataitem>'
        self.inner = '<recordinfo><datalist><dataitem datatype="1"><datadesc>unrelated text is not an attachment</datadesc></dataitem>'+self.file_xml+'</datalist></recordinfo>'
        self.message = self.message_for(self.inner)

    def tearDown(self):self.temp.cleanup()

    def message_for(self, record):
        return {'server_id': 9007199254742007, 'sort_seq': 1700000000000,
                'content': {'MergedMessages': {'raw_xml':'<msg><appmsg><recorditem>'+html.escape(record)+'</recorditem></appmsg></msg>'}}}

    def item(self):return merged.file_items(self.message)[0]

    def test_metadata_preserves_original_id_as_string_and_hides_cdn_credentials(self):
        item = self.item()
        self.assertEqual(item['itemPath'], [1])
        self.assertEqual(item['originalMessageId'], '9007199254741009')
        self.assertEqual(item['expectedMd5'], self.md5)
        self.assertNotIn('PRIVATE_CDN', str(item))
        self.assertEqual(merged.parent_identity(self.message, 'group')['serverId'], '9007199254742007')

    def test_nested_record_preserves_hierarchical_path(self):
        nested = '<recordinfo><datalist><dataitem datatype="17"><recordxml>'+html.escape(self.inner)+'</recordxml></dataitem></datalist></recordinfo>'
        items = merged.file_items(self.message_for(nested))
        self.assertEqual(items[0]['itemPath'], [0, 1])
        self.assertEqual(merged.select(items, [0, 1])['fileName'], 'guide.pdf')
        with self.assertRaises(merged.AttachmentError):merged.select(items, [1])

    def test_missing_original_is_unavailable_not_metadata_success(self):
        result = merged.export(self.account, self.root / 'media', self.message, 'group', self.item())
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['error'], 'merged_file_not_downloaded')
        self.assertFalse(result['originalFileObtained'])
        self.assertNotIn('path', result)

    def test_thumbnail_and_same_name_wrong_file_are_not_originals(self):
        (self.account / 'guide.pdf').write_bytes(b'thumbnail or incorrect content')
        result = merged.export(self.account, self.root / 'media', self.message, 'group', self.item())
        self.assertEqual(result['error'], 'merged_file_integrity_mismatch')
        self.assertFalse(result['originalFileObtained'])

    def test_exact_hash_original_with_nonstandard_cache_name_is_exported(self):
        source = self.account / 'opaque-record-cache-name'
        source.write_bytes(self.payload)
        result = merged.export(self.account, self.root / 'media', self.message, 'group', self.item())
        self.assertEqual(result['status'], 'success')
        self.assertEqual(Path(result['path']).read_bytes(), self.payload)
        self.assertEqual(result['md5'], self.md5)
        self.assertEqual(result['bytes'], len(self.payload))
        self.assertEqual(Path(result['path']).stat().st_mode & 0o777, 0o600)
        self.assertTrue(result['md5Verified'])

    def test_symlink_outside_account_is_not_read(self):
        outside = self.root / 'original.pdf'
        outside.write_bytes(self.payload)
        (self.account / 'guide.pdf').symlink_to(outside)
        result = merged.export(self.account, self.root / 'media', self.message, 'group', self.item())
        self.assertFalse(result['originalFileObtained'])

    def test_verified_export_can_be_reused_without_a_client_download_copy(self):
        source = self.account / 'client-original'
        source.write_bytes(self.payload)
        first = merged.export(self.account, self.root / 'media', self.message, 'group', self.item())
        source.unlink()
        second = merged.acquire(self.account, self.root / 'media', self.message, 'group', self.item(), allow_download=False)
        self.assertEqual(second['path'], first['path'])
        self.assertTrue(second['md5Verified'])
        self.assertFalse(second['networkDownloadPerformed'])

    def test_path_traversal_and_xml_entities_are_rejected(self):
        for raw in (self.inner.replace('guide.pdf', '../guide.pdf'), '<!DOCTYPE x [<!ENTITY a "expanded">]><recordinfo/>'):
            with self.assertRaises(merged.AttachmentError):merged.file_items(self.message_for(raw))

    def test_bridge_queries_parent_only_and_never_uses_inner_original_id(self):
        with patch.object(bridge, 'paged_query', return_value={'items':[self.message]}) as query:
            contact, message = bridge.merged_parent({'contact':'group', 'parentServerId':'9007199254742007'})
            self.assertEqual(query.call_args.args[1]['aroundServerId'], 9007199254742007)
            result = bridge.merged_attachment('wechat_list_merged_attachments', {'contact':'group', 'parentSortSeq':1700000000000, 'fileNames':['guide.pdf']})
            self.assertEqual(query.call_args.args[1]['aroundSortSeq'], 1700000000000)
            self.assertNotIn(9007199254741009, query.call_args.args[1].values())
            self.assertEqual(len(result['items']), 1)
            self.assertFalse(result['innerIdsAreParentConversationMessageIds'])

    def test_outer_http_415_reports_missing_inner_selector(self):
        from backends.receive import local_service as service
        with patch.object(bridge, 'account_args', return_value=['--account','fixture']), patch.object(bridge, 'database_root'), patch.object(service, 'ensure', return_value={}), patch.object(service, 'media', side_effect=service.ServiceError('local_service_http_415')), patch.object(bridge, 'merged_parent', return_value=('group',self.message)):
            with self.assertRaises(bridge.BridgeError) as error:
                bridge.service_read('wechat_read_media', {'contact':'group','serverId':9007199254742007})
            self.assertEqual(error.exception.code, 'merged_attachment_selector_required')


if __name__ == '__main__':unittest.main()

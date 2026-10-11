import json
import os
from pathlib import Path
import tempfile
import unittest

import runtime_sender as sender


class ApprovalAndOneShotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.proposal={'schema':1,'kind':'send_text','recipient':'filehelper','text':'fixture only, never sent',
            'timestamp':1791646458,'client_message_id':7,'maximum_sends':1,'gui_input':False}
        self.approval={'schema':1,'human_approved':True,'proposal_sha256':sender.proposal_digest(self.proposal),'expires_at':1500}
        self.proposal_path=self.root/'proposal.json';self.approval_path=self.root/'approval.json'
        self.save()
    def tearDown(self):
        self.temp.cleanup()
    def save(self):
        for path,data in ((self.proposal_path,self.proposal),(self.approval_path,self.approval)):
            path.write_text(json.dumps(data));path.chmod(0o600)
    def load(self):
        return sender.load_approved_proposal(self.proposal_path,self.approval_path,clock=lambda:1000)
    def test_exact_reviewed_proposal(self):
        self.assertEqual(self.load(),self.proposal)
    def test_approval_does_not_authorize_changed_body(self):
        self.proposal['text']='changed after review';self.save()
        with self.assertRaises(sender.Guard):self.load()
    def test_cannot_redirect_test_to_someone_else(self):
        self.proposal['recipient']='somebody';self.approval['proposal_sha256']=sender.proposal_digest(self.proposal);self.save()
        with self.assertRaises(sender.Guard):self.load()
    def test_expired_or_unapproved_rejected(self):
        for update in ({'expires_at':999},{'human_approved':False},{'expires_at':5001}):
            with self.subTest(update=update):
                approval=dict(self.approval);approval.update(update)
                self.approval_path.write_text(json.dumps(approval));self.approval_path.chmod(0o600)
                with self.assertRaises(sender.Guard):self.load()
    def test_symlink_approval_refused(self):
        other=self.root/'approval-original.json';self.approval_path.rename(other);self.approval_path.symlink_to(other)
        with self.assertRaises(sender.Guard):self.load()
    def test_unknown_enqueue_cannot_be_claimed_again(self):
        claim=self.root/'claim.json';sender.claim_once(claim,self.proposal)
        with self.assertRaises(FileExistsError):sender.claim_once(claim,self.proposal)
        saved=json.loads(claim.read_text())
        self.assertFalse(saved['automatic_retry_allowed'])
        self.assertEqual(os.stat(claim).st_mode&0o777,0o600)
        self.assertNotIn('text',saved)

if __name__=='__main__':unittest.main()

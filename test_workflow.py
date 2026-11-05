import copy
import json
import tempfile
import unittest
import uuid
from datetime import date
from email import policy
from email.message import EmailMessage
from email.mime.image import MIMEImage
from pathlib import Path
from unittest.mock import patch
import core
import examples
from pricelist import Engine, AutomationStop, parse, digest

DAY = date(2026,9,8)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.e = Engine(Path(self.tmp.name)/'state', examples.CONFIG)
        self.addCleanup(lambda: self.e.__exit__())

    def initial(self):
        return self.e.initial(DAY)[0]['message_id']

    def sent(self):
        draft = self.initial()
        self.e.mark_sent(draft,'<sent-request@office.example>','2026-09-08T09:00:00+00:00')
        return draft

    def received(self, raw=None):
        self.sent()
        return self.e.receive(raw or examples.reply(), date(2026,9,9))

    def receipt(self, status='SUCCESS', receipt_id='import-1'):
        r = self.e.data['messages']['<reply@supplier.example>']
        return dict(receipt_id=receipt_id,message_id='<reply@supplier.example>',supplier=r['supplier'],cycle=r['cycle'],sha256=r['sha256'],status=status)

    def test_monthly_calendar_and_weekends(self):
        self.assertEqual(core._first_business_day_on_or_after_eighth(date(2026,11,1)),date(2026,11,9))
        self.assertEqual(core._add_business_days(date(2026,9,11),2),date(2026,9,15))
        self.assertEqual(self.e.initial(date(2026,9,7))['status'],'NO_OP')

    def test_initial_draft_readback_then_idempotent_repeat(self):
        d = self.initial()
        self.assertEqual(self.e.initial(DAY)[0]['status'],'NO_OP')
        self.assertEqual(len(self.e.data['drafts']),1)
        self.assertEqual(self.e.data['cycles']['2026-09/demo']['status'],'DRAFT')
        self.assertTrue(self.e.data['drafts'][d]['pending'])
        self.assertNotIn('sent_id', self.e.data['drafts'][d])

    def test_draft_is_not_sent_and_cannot_match_reply(self):
        self.initial()
        self.assertEqual(self.e.receive(examples.reply(),date(2026,9,9))['status'],'REVIEW')
        self.assertEqual(self.e.reminders(date(2026,9,10))[0]['reason'],'request_not_marked_sent')

    def test_mark_sent_reassigned_id_and_replay(self):
        d = self.sent()
        self.assertEqual(self.e.mark_sent(d,'<sent-request@office.example>','2026-09-08T09:00:00+00:00')['status'],'NO_OP')
        self.assertEqual(self.e.receive(examples.reply(),date(2026,9,9))['status'],'RECEIVED')

    def test_sent_fact_conflict(self):
        d = self.sent()
        with self.assertRaisesRegex(AutomationStop,'conflict'):
            self.e.mark_sent(d,'<different@office.example>','2026-09-08T09:00:00+00:00')

    def test_no_thanks_without_successful_import(self):
        self.received()
        self.assertEqual(self.e.thanks('<reply@supplier.example>',date(2026,9,9))['status'],'REVIEW')
        self.assertEqual(len(self.e.data['drafts']),1)

    def test_success_receipt_thanks_and_no_importer(self):
        self.received()
        r = self.receipt()
        self.assertEqual(self.e.ack_import(r)['status'],'IMPORTED')
        self.assertEqual(self.e.ack_import(r)['status'],'NO_OP')
        out = self.e.thanks('<reply@supplier.example>',date(2026,9,9))
        self.assertEqual(out['kind'],'thanks')
        m = parse(self.e.read_draft(self.e.data['drafts'][out['message_id']]))
        self.assertEqual(str(m['In-Reply-To']),'<reply@supplier.example>')
        self.assertIn('Спасибо большое, взял в работу!',m.get_content())
        self.assertEqual(len(list(m.iter_attachments())),0)
        self.assertEqual(self.e.thanks('<reply@supplier.example>',date(2026,9,9))['status'],'NO_OP')
        self.assertEqual(self.e.reminders(date(2026,9,10))['status'],'NO_OP')

    def test_failed_import_retains_lineage_then_success(self):
        self.received()
        self.e.ack_import(self.receipt('FAILED','failure-1'))
        self.assertEqual(self.e.data['messages']['<reply@supplier.example>']['status'],'RECEIVED')
        self.assertEqual(self.e.thanks('<reply@supplier.example>',date(2026,9,9))['status'],'REVIEW')
        self.e.ack_import(self.receipt('SUCCESS','success-2'))
        self.assertEqual(len(self.e.data['acks']),2)

    def test_import_receipt_wrong_hash(self):
        self.received()
        r = self.receipt()
        r['sha256'] = '0'*64
        with self.assertRaisesRegex(AutomationStop,'binding'):
            self.e.ack_import(r)

    def test_two_business_days_and_pendingDraft(self):
        self.sent()
        self.assertEqual(self.e.reminders(date(2026,9,9))['status'],'NO_OP')
        first = self.e.reminders(date(2026,9,10))[0]
        self.assertEqual(first['number'],1)
        self.assertEqual(self.e.reminders(date(2026,9,14))[0]['reason'],'pendingDraft')
        self.assertEqual(len(self.e.data['drafts']),2)

    def test_three_reminders_then_escalation_without_fourth_draft(self):
        # Adapted original scenario: 10,14,16 September; escalation on 18th.
        self.sent()
        for n,day in enumerate((10,14,16),1):
            out = self.e.reminders(date(2026,9,day))[0]
            self.assertEqual(out['number'],n)
            self.e.mark_sent(out['message_id'],f'<reminder{n}@office.example>',f'2026-09-{day:02d}T10:00:00+00:00')
        self.assertEqual(self.e.reminders(date(2026,9,18))[0]['status'],'ESCALATED')
        self.assertEqual(len(self.e.data['drafts']),4)
        self.assertEqual(self.e.reminders(date(2026,9,21))['status'],'NO_OP')

    def test_reminder_preserves_full_reference_chain(self):
        self.sent()
        first = self.e.reminders(date(2026,9,10))[0]['message_id']
        self.e.mark_sent(first,'<reminder1@office.example>','2026-09-10T10:00:00+00:00')
        second = self.e.reminders(date(2026,9,14))[0]['message_id']
        m = parse(self.e.read_draft(self.e.data['drafts'][second]))
        self.assertEqual(str(m['References']),'<sent-request@office.example> <reminder1@office.example>')
        self.assertEqual(str(m['In-Reply-To']),'<sent-request@office.example>')
        self.assertIn('Ранее запрашивал',m.get_content())
        self.assertIn('Прошу направить актуальный',m.get_content())

    def test_missing_thread_is_review_not_standalone(self):
        self.sent()
        self.e.data['cycles']['2026-09/demo']['sent'] = []
        self.assertEqual(self.e.reminders(date(2026,9,10))[0]['status'],'REVIEW')
        self.assertEqual(len(self.e.data['drafts']),1)

    def test_wrong_sender_old_date_future_date_and_wrong_thread(self):
        self.sent()
        for kwargs in ({'sender':'other@supplier.example'},{'parent':'<unknown@office.example>'},{'when':'Mon, 7 Sep 2026 10:00:00 +0000'},{'when':'Thu, 10 Sep 2026 10:00:00 +0000'}):
            raw = examples.reply(message_id=f'<{uuid.uuid4().hex}@supplier.example>', **kwargs)
            self.assertEqual(self.e.receive(raw,date(2026,9,9))['status'],'REVIEW')

    def test_backup_address_is_accepted(self):
        self.sent()
        self.assertEqual(self.e.receive(examples.reply(sender='backup@supplier.example'),date(2026,9,9))['status'],'RECEIVED')

    def test_reply_before_parent_sent_is_review(self):
        self.sent()
        raw=examples.reply(when='Tue, 8 Sep 2026 08:00:00 +0000')
        self.assertEqual(self.e.receive(raw,date(2026,9,9))['status'],'REVIEW')

    def test_replay_no_new_events_and_immutable_raw(self):
        self.received()
        count=len(self.e.data['events'])
        self.assertEqual(self.e.receive(examples.reply(),date(2026,9,9))['status'],'NO_OP')
        self.assertEqual(len(self.e.data['events']),count)
        r=self.e.data['messages']['<reply@supplier.example>']
        self.assertEqual(self.e.read_checked(r['attachment_path'],r['sha256']),examples.xlsx())
        self.assertEqual((self.e.root/r['attachment_path']).stat().st_mode & 0o222,0)

    def test_message_id_collision_does_not_overwrite_receipt(self):
        self.received()
        old=copy.deepcopy(self.e.data['messages']['<reply@supplier.example>'])
        self.assertEqual(self.e.receive(examples.reply(payload=b'not a workbook'),date(2026,9,9))['status'],'REVIEW')
        self.assertEqual(self.e.data['messages']['<reply@supplier.example>'],old)

    def test_explicit_manual_retry_requires_new_uuid_and_is_not_normal_repeat(self):
        self.initial()
        raw=examples.reply()
        self.e.receive(raw,date(2026,9,9))
        self.assertEqual(self.e.receive(raw,date(2026,9,9))['status'],'NO_OP')
        self.e.mark_sent(self.e.data['cycles']['2026-09/demo']['initial'],'<sent-request@office.example>','2026-09-08T09:00:00+00:00')
        token=str(uuid.uuid4())
        self.assertEqual(self.e.receive(raw,date(2026,9,9),token)['status'],'RECEIVED')
        with self.assertRaises(AutomationStop):
            self.e.receive(raw,date(2026,9,9),token)
        self.assertTrue(any(x['status']=='REVIEW' for x in self.e.data['events']))

    def test_real_xls_not_lost(self):
        self.sent()
        raw=examples.reply(filename='prices.xls',payload=examples.xls())
        result=self.e.receive(raw,date(2026,9,9))
        self.assertEqual(result['status'],'RECEIVED',result)
        self.assertEqual(self.e.read_checked(result['attachment_path'],result['sha256']),examples.xls())

    def _inbound(self,sender,message_id,filename='prices.xlsx'):
        return examples.reply(sender=sender,message_id=message_id,filename=filename)

    def test_filename_traversal_is_blocked(self) -> None:
        # Original test case retained, synthetic input helper now supplies XLSX.
        import email
        automation = core
        message = email.message_from_bytes(self._inbound('a@test.example', '<x@test.example>', filename='../evil.csv'))
        with self.assertRaisesRegex(automation.AutomationStop, 'attachment_filename_unsafe'):
            automation.extract_single_attachment(message)

    def test_inline_signature_image_is_not_counted_as_second_attachment(self):
        # Adapted original attachment-selection test, XLSX replaces CSV.
        import email
        message = email.message_from_bytes(self._inbound('prices@supplier.example','<inline@supplier.example>'))
        logo = MIMEImage(b'png-signature-bytes',_subtype='png')
        logo.add_header('Content-Disposition','inline',filename='image001.png')
        message.attach(logo)
        filename,payload=core.extract_single_attachment(message)
        self.assertEqual(filename,'prices.xlsx')
        self.assertEqual(payload,examples.xlsx())

    def test_duplicate_headers_review(self):
        self.sent()
        raw = examples.reply().replace(b'MIME-Version: 1.0',b'Message-ID: <extra@supplier.example>\r\nMIME-Version: 1.0',1)
        self.assertEqual(self.e.receive(raw,date(2026,9,9))['status'],'REVIEW')

    def test_header_injection_config_blocked(self):
        from pricelist import address, clean, mid
        for f,v in ((address,'a@supplier.example\r\nBcc: leak@bad.example'),(clean,'Name\nInjected'),(mid,'<a@b.example>\nCc: x')):
            with self.assertRaises(AutomationStop):
                f(v)

    def test_no_network(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            self.received()
            self.e.ack_import(self.receipt())
            self.e.thanks('<reply@supplier.example>',date(2026,9,9))

    def test_tampered_raw_blocks_thanks_without_reimport(self):
        self.received()
        self.e.ack_import(self.receipt())
        r=self.e.data['messages']['<reply@supplier.example>']
        p=self.e.root/r['raw_path']
        p.chmod(0o600)
        p.write_bytes(b'modified')
        with self.assertRaisesRegex(AutomationStop,'hash_mismatch'):
            self.e.thanks('<reply@supplier.example>',date(2026,9,9))
        self.assertEqual(r['status'],'IMPORTED')

    def test_state_survives_reopen(self):
        self.received()
        self.e.__exit__()
        self.e=Engine(self.e.root,examples.CONFIG)
        self.assertEqual(self.e.receive(examples.reply(),date(2026,9,9))['status'],'NO_OP')

    def test_symlink_output_blocked(self):
        dest=self.e.root/'output'
        dest.symlink_to(Path(self.tmp.name),target_is_directory=True)
        with self.assertRaisesRegex(AutomationStop,'symlink'):
            self.initial()

    def test_delayed_owner_send_reminder(self):
        d=self.initial()
        self.e.mark_sent(d,'<sent-request@office.example>','2026-09-09T09:00:00+00:00')
        result=self.e.reminders(date(2026,9,11))
        self.assertEqual(result[0]['status'],'DRAFT',result)

    def test_orphan_reminder_recovers_original_bytes_next_day(self):
        self.sent()
        before = copy.deepcopy(self.e.data)
        first = self.e.reminders(date(2026,9,10))[0]['message_id']
        raw = self.e.read_draft(self.e.data['drafts'][first])
        self.e.data = before  # power loss before tracker commit; output already durable
        out = self.e.reminders(date(2026,9,11))[0]
        self.assertEqual(out['message_id'],first)
        self.assertEqual(self.e.read_draft(self.e.data['drafts'][first]),raw)
        self.assertEqual(self.e.data['drafts'][first]['created'],'2026-09-10')

    def test_delayed_reminder_send_waits_two_business_days(self):
        self.sent()
        first = self.e.reminders(date(2026,9,10))[0]['message_id']
        self.e.mark_sent(first,'<reminder1@office.example>','2026-09-15T10:00:00+00:00')
        self.assertEqual(self.e.reminders(date(2026,9,15))['status'],'NO_OP')
        self.assertEqual(self.e.reminders(date(2026,9,17))[0]['number'],2)

    def test_orphan_thanks_recovers_without_import_repeat(self):
        self.received()
        self.e.ack_import(self.receipt())
        before=copy.deepcopy(self.e.data)
        out=self.e.thanks('<reply@supplier.example>',date(2026,9,9))
        raw=self.e.read_draft(self.e.data['drafts'][out['message_id']])
        self.e.data=before
        recovered=self.e.thanks('<reply@supplier.example>',date(2026,9,10))
        self.assertEqual(recovered['message_id'],out['message_id'])
        self.assertEqual(self.e.read_draft(self.e.data['drafts'][out['message_id']]),raw)


if __name__=='__main__':
    unittest.main()

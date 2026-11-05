#!/usr/bin/env python3
"""Offline draft-only adapter around the extracted request workflow."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import date, datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
from pathlib import Path
from core import (AutomationStop, Target, _first_business_day_on_or_after_eighth,
                  _add_business_days, _cycle_key, deterministic_message_id,
                  _subject, _body, _message_text, message_id_tokens,
                  select_request_thread, extract_single_attachment)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def clean(value):
    if not isinstance(value, str) or not value or len(value) > 240 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AutomationStop('unsafe_text')
    return value


def address(value):
    clean(value)
    if not re.fullmatch(r'[A-Za-z0-9.!#$%+_=-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value):
        raise AutomationStop('invalid_address')
    return value.casefold()


def mid(value):
    clean(value)
    if not re.fullmatch(r'<[A-Za-z0-9.!#$%+_=/~-]+@[A-Za-z0-9.-]+>', value):
        raise AutomationStop('invalid_message_id')
    return value


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise AutomationStop('timezone_required')
    return result.astimezone(timezone.utc)


def parse(raw):
    if len(raw) > 16 * 1024 * 1024:
        raise AutomationStop('message_size_limit')
    m = BytesParser(policy=policy.default).parsebytes(raw)
    for h in ('From', 'To', 'Date', 'Message-ID', 'Subject', 'In-Reply-To', 'References'):
        if len(m.get_all(h, [])) > 1:
            raise AutomationStop('duplicate_header:' + h)
        if m.get(h) is not None:
            clean(str(m[h]))
    if len(list(m.walk())) > 64 or any(p.defects for p in m.walk()):
        raise AutomationStop('mime_malformed_or_parts_limit')
    return m


def one_address(m, header):
    rows = getaddresses(m.get_all(header, []))
    if len(rows) != 1:
        raise AutomationStop('exactly_one_address_required:' + header)
    return address(rows[0][1])


def safe_dir(path):
    path = Path(path).absolute()
    for p in [path, *path.parents]:
        if p.is_symlink():
            raise AutomationStop('symlink_directory_blocked')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def immutable(path, data):
    safe_dir(path.parent)
    if path.is_symlink():
        raise AutomationStop('symlink_file_blocked')
    if path.exists():
        if path.read_bytes() != data:
            raise AutomationStop('immutable_content_conflict')
        return
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o400)
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise AutomationStop('immutable_content_conflict')
        fsync_dir(path.parent)
    finally:
        os.unlink(temp)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Engine:
    def __init__(self, state, config):
        self.root = safe_dir(state)
        fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, 'w')
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            self.config = config
            self.sender = address(config['sender'])
            self.suppliers = {}
            emails = set()
            for s in config['suppliers']:
                key = clean(s['id'])
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key) or key in self.suppliers:
                    raise AutomationStop('supplier_id_invalid_or_duplicate')
                clean(s['name'])
                addrs = tuple(address(x) for x in s['emails'])
                if not addrs or len(set(addrs)) != len(addrs) or emails.intersection(addrs):
                    raise AutomationStop('supplier_address_missing_or_ambiguous')
                emails.update(addrs)
                self.suppliers[key] = dict(s, emails=addrs)
            if not self.suppliers:
                raise AutomationStop('suppliers_empty')
            self.path = self.root / 'tracker.json'
            if self.path.is_symlink():
                raise AutomationStop('symlink_tracker_blocked')
            self.data = json.loads(self.path.read_text()) if self.path.exists() else {'version':1, 'cycles':{}, 'messages':{}, 'drafts':{}, 'events':[], 'retries':[], 'acks':{}}
            fingerprint = digest(json.dumps(config, sort_keys=True, ensure_ascii=False).encode())
            if self.data.get('config_hash', fingerprint) != fingerprint:
                raise AutomationStop('config_changed_use_new_state')
            self.data['config_hash'] = fingerprint
        except BaseException:
            self.lock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.lock.close()

    def save(self, event):
        self.data['events'].append(event)
        fd, temp = tempfile.mkstemp(dir=self.root, prefix='.tracker-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
            fsync_dir(self.root)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return event

    def target(self, key, cycle, count=0):
        s = self.suppliers[key]
        return Target(key, s['name'], s['emails'], cycle, count)

    def make_draft(self, key, day, kind, count=0, thread=(), inbound=None):
        target = self.target(key, _cycle_key(day), max(0, count-1))
        message_id = (f'<thanks-{digest(str(inbound).encode())[:32]}@automation.example>' if kind == 'thanks'
                      else deterministic_message_id(target, target.cycle_key, kind, count))
        path = self.root / 'output' / (digest(message_id.encode()) + '.eml')
        safe_dir(path.parent)
        if path.is_symlink():
            raise AutomationStop('symlink_file_blocked')
        if path.exists():
            # Recover durable orphan output after a crash before tracker commit.
            previous = parse(path.read_bytes())
            created = parsedate_to_datetime(str(previous['Date']))
            if created.tzinfo is None or not _first_business_day_on_or_after_eighth(day) <= created.date() <= day:
                raise AutomationStop('orphan_draft_date_invalid')
            day = created.date()
        m = EmailMessage(policy=policy.SMTP)
        m['From'] = self.sender
        m['To'] = target.emails[0] if inbound is None else self.data['messages'][inbound]['sender']
        m['Date'] = format_datetime(datetime.combine(day, datetime.min.time(), timezone.utc))
        m['Message-ID'] = mid(message_id)
        subject = _subject(_first_business_day_on_or_after_eighth(day), target.supplier_name)
        m['Subject'] = subject if kind == 'initial' else 'RE: ' + subject
        m['X-Pricelist-Reminder-Number'] = str(count)
        m['X-Unsent'] = '1'
        body = 'Спасибо большое, взял в работу!' if kind == 'thanks' else _body(kind, count)
        if thread:
            m['In-Reply-To'] = mid(str(parse(thread[0])['Message-ID']))
            refs = [mid(str(parse(raw)['Message-ID'])) for raw in thread]
            m['References'] = ' '.join(refs)
            # Only plain text, never replay supplier HTML/remote resources.
            body += '\n\n' + '\n\n'.join('> ' + _message_text(parse(raw)).replace('\n', '\n> ') for raw in reversed(thread))
        m.set_content(body, charset='utf-8')
        raw = m.as_bytes()
        immutable(path, raw)
        checked = parse(path.read_bytes())
        if str(checked['Message-ID']) != message_id or one_address(checked, 'To') != str(m['To']):
            raise AutomationStop('draft_readback_failed')
        self.data['drafts'].setdefault(message_id, {'supplier':key, 'cycle':target.cycle_key, 'kind':kind, 'number':count, 'created':day.isoformat(), 'pending':True, 'path':str(path.relative_to(self.root)), 'sha256':digest(raw)})
        return message_id

    def initial(self, day):
        if day != _first_business_day_on_or_after_eighth(day):
            return {'status':'NO_OP', 'reason':'not_monthly_request_day'}
        results = []
        for key in self.suppliers:
            cycle_key = _cycle_key(day) + '/' + key
            if cycle_key in self.data['cycles']:
                results.append({'supplier':key, 'status':'NO_OP'})
                continue
            msgid = self.make_draft(key, day, 'initial')
            self.data['cycles'][cycle_key] = {'supplier':key, 'cycle':_cycle_key(day), 'initial':msgid, 'reminders':[], 'sent':[], 'status':'DRAFT'}
            results.append(self.save({'status':'DRAFT', 'supplier':key, 'message_id':msgid}))
        return results

    def mark_sent(self, draft_id, actual_id, sent_at):
        mid(actual_id)
        when = timestamp(sent_at)
        d = self.data['drafts'][draft_id]
        if not d['pending']:
            if d.get('sent_id') == actual_id and d.get('sent_at') == when.isoformat():
                return {'status':'NO_OP'}
            raise AutomationStop('sent_fact_conflict')
        if when.date() < date.fromisoformat(d['created']) or _cycle_key(when.date()) != d['cycle']:
            raise AutomationStop('sent_date_outside_cycle')
        if any(x.get('sent_id') == actual_id for x in self.data['drafts'].values()):
            raise AutomationStop('sent_id_already_used')
        raw = self.read_draft(d)
        m = parse(raw)
        m.replace_header('Message-ID', actual_id)
        m.replace_header('Date', format_datetime(when))
        del m['X-Unsent']
        sent_raw = m.as_bytes(policy=policy.SMTP)
        path = self.root / 'sent' / (digest(actual_id.encode())+'.eml')
        immutable(path, sent_raw)
        d.update(pending=False, sent_id=actual_id, sent_at=when.isoformat(), sent_path=str(path.relative_to(self.root)), sent_hash=digest(sent_raw))
        c = self.data['cycles'][d['cycle']+'/'+d['supplier']]
        if d['kind'] != 'thanks':
            c['sent'].append(draft_id)
            if d['kind'] == 'initial':
                c['status'] = 'SENT'
        return self.save({'status':'SENT_CONFIRMED_BY_OWNER', 'draft_id':draft_id, 'message_id':actual_id, 'sent_at':when.isoformat()})

    def read_draft(self, d):
        return self.read_checked(d['path'], d['sha256'])

    def read_checked(self, name, sha):
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise AutomationStop('state_path_outside_root')
        p = self.root / name
        if p.is_symlink() or any(x.is_symlink() for x in p.parents):
            raise AutomationStop('state_symlink_blocked')
        data = p.read_bytes()
        if digest(data) != sha:
            raise AutomationStop('saved_raw_hash_mismatch')
        return data

    def reminders(self, day):
        results = []
        for key in self.suppliers:
            c = self.data['cycles'].get(_cycle_key(day)+'/'+key)
            if not c or day.weekday() >= 5 or c['status'] == 'IMPORTED':
                continue
            initial = self.data['drafts'][c['initial']]
            if initial['pending']:
                results.append({'status':'REVIEW', 'reason':'request_not_marked_sent', 'supplier':key})
                continue
            if c['reminders'] and self.data['drafts'][c['reminders'][-1]]['pending']:
                results.append({'status':'NO_OP', 'reason':'pendingDraft', 'supplier':key})
                continue
            previous = self.data['drafts'][c['reminders'][-1]] if c['reminders'] else initial
            start = max(previous['created'], previous['sent_at'][:10])
            if day < _add_business_days(date.fromisoformat(start), 2):
                continue
            if len(c['reminders']) == 3:
                if c['status'] != 'ESCALATED':
                    c['status'] = 'ESCALATED'
                    results.append(self.save({'status':'ESCALATED', 'supplier':key}))
                continue
            thread = [self.read_checked(self.data['drafts'][d]['sent_path'], self.data['drafts'][d]['sent_hash']) for d in c['sent']]
            try:
                thread = select_request_thread(thread, self.target(key, c['cycle'], len(c['reminders'])), day)
            except AutomationStop as e:
                results.append({'status':'REVIEW', 'supplier':key, 'reason':str(e)})
                continue
            number = len(c['reminders'])+1
            draft = self.make_draft(key, day, 'reminder', number, thread)
            c['reminders'].append(draft)
            results.append(self.save({'status':'DRAFT', 'kind':'reminder', 'number':number, 'message_id':draft, 'supplier':key}))
        return results or {'status':'NO_OP'}

    def receive(self, raw, day, retry=None):
        if len(raw) > 16 * 1024 * 1024:
            raise AutomationStop('message_size_limit')
        raw_hash = digest(raw)
        key = 'invalid:' + raw_hash
        reason = 'message_invalid'
        folder = Path('raw') / raw_hash
        immutable(self.root / folder / 'message.eml', raw)
        try:
            m = parse(raw)
            key = mid(str(m.get('Message-ID', '')))
        except (ValueError, AutomationStop) as e:
            m = None
            reason = str(e)
        old = self.data['messages'].get(key)
        if old:
            if old['raw_hash'] != raw_hash:
                return self.save({'status':'REVIEW', 'reason':'message_id_content_collision', 'message_id':key, 'raw_hash':raw_hash})
            if retry is None:
                return {'status':'NO_OP', 'reason':'already_seen', 'previous_status':old['status'], 'message_id':key}
            token = str(uuid.UUID(retry))
            if token in self.data['retries'] or old['status'] != 'REVIEW':
                raise AutomationStop('retry_not_allowed_or_token_used')
            self.data['retries'].append(token)
        elif retry is not None:
            raise AutomationStop('retry_requires_existing_review')
        record = {'raw_hash':raw_hash, 'raw_path':str(folder/'message.eml'), 'status':'REVIEW'}
        try:
            if m is None:
                raise AutomationStop(reason)
            sender = one_address(m, 'From')
            if one_address(m, 'To') != self.sender:
                raise AutomationStop('wrong_recipient')
            candidates = [k for k,s in self.suppliers.items() if sender in s['emails']]
            if len(candidates) != 1:
                raise AutomationStop('sender_unknown_or_ambiguous')
            supplier = candidates[0]
            c = self.data['cycles'].get(_cycle_key(day)+'/'+supplier)
            if not c or not c['sent']:
                raise AutomationStop('no_explicit_sent_request')
            received = parsedate_to_datetime(str(m['Date']))
            if received.tzinfo is None:
                raise AutomationStop('timezone_required')
            received = received.astimezone(timezone.utc)
            initial = self.data['drafts'][c['initial']]
            if not timestamp(initial['sent_at']) <= received or received.date() > day or _cycle_key(received.date()) != c['cycle']:
                raise AutomationStop('reply_date_outside_cycle')
            known = {self.data['drafts'][x]['sent_id']:self.data['drafts'][x] for x in c['sent']}
            direct = message_id_tokens(str(m.get('In-Reply-To', '')))
            ancestors = message_id_tokens(str(m.get('References', '')))
            matched = direct.intersection(known) if direct else ancestors.intersection(known)
            if len(matched) != 1:
                raise AutomationStop('reply_thread_not_exact_or_ambiguous')
            if received < timestamp(known[next(iter(matched))]['sent_at']):
                raise AutomationStop('reply_before_parent_sent')
            name, payload = extract_single_attachment(m)
            # Fixed stored filename avoids collisions with message.eml and manifests.
            attachment_path = folder / ('attachment' + Path(name).suffix.casefold())
            immutable(self.root / attachment_path, payload)
            record.update(status='RECEIVED', supplier=supplier, cycle=c['cycle'], sender=sender,
                          attachment_path=str(attachment_path), filename=name, sha256=digest(payload), received_at=received.isoformat())
        except Exception as e:
            record['reason'] = str(e)
        self.data['messages'][key] = record
        return self.save(dict(record, message_id=key, retry=retry))

    def ack_import(self, receipt):
        rid = clean(receipt['receipt_id'])
        if rid in self.data['acks']:
            if self.data['acks'][rid] != receipt:
                raise AutomationStop('import_receipt_id_conflict')
            return {'status':'NO_OP'}
        key = mid(receipt['message_id'])
        r = self.data['messages'][key]
        if r['status'] not in {'RECEIVED', 'IMPORTED'} or any(receipt[k] != r[k] for k in ('supplier', 'cycle', 'sha256')):
            raise AutomationStop('import_receipt_binding_mismatch')
        if receipt['status'] not in {'SUCCESS', 'FAILED'}:
            raise AutomationStop('import_receipt_status_invalid')
        self.read_checked(r['attachment_path'], r['sha256'])
        if r['status'] == 'IMPORTED' and receipt['status'] != 'SUCCESS':
            raise AutomationStop('cannot_revoke_import_with_failure')
        self.data['acks'][rid] = receipt
        if receipt['status'] == 'SUCCESS':
            r['status'] = 'IMPORTED'
            self.data['cycles'][r['cycle']+'/'+r['supplier']]['status'] = 'IMPORTED'
        return self.save({'status':'IMPORTED' if receipt['status']=='SUCCESS' else 'IMPORT_FAILED', 'receipt_id':rid, 'message_id':key})

    def thanks(self, key, day):
        r = self.data['messages'][key]
        if r['status'] != 'IMPORTED':
            return {'status':'REVIEW', 'reason':'successful_import_receipt_required'}
        if _cycle_key(day) != r['cycle']:
            raise AutomationStop('thanks_cycle_mismatch')
        if r.get('thanks'):
            return {'status':'NO_OP', 'message_id':r['thanks']}
        raw = self.read_checked(r['raw_path'], r['raw_hash'])
        msgid = self.make_draft(r['supplier'], day, 'thanks', thread=[raw], inbound=key)
        r['thanks'] = msgid
        return self.save({'status':'DRAFT', 'kind':'thanks', 'message_id':msgid, 'parent':key})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--suppliers', type=Path, required=True)
    sub = p.add_subparsers(dest='command', required=True)
    for cmd in ('initial', 'reminders'):
        sub.add_parser(cmd).add_argument('--today', type=date.fromisoformat, required=True)
    sub.add_parser('status')
    sent = sub.add_parser('mark-sent', help='Owner attestation only; does not send mail')
    sent.add_argument('--draft-id', required=True)
    sent.add_argument('--message-id', required=True)
    sent.add_argument('--sent-at', required=True)
    incoming = sub.add_parser('receive', help='Read local .eml or maildir new/cur; never changes input')
    incoming.add_argument('--input', type=Path, required=True)
    incoming.add_argument('--today', type=date.fromisoformat, required=True)
    retry = sub.add_parser('retry', help='Recheck one saved REVIEW message; no external effects')
    retry.add_argument('--message-id', required=True)
    retry.add_argument('--token', required=True)
    retry.add_argument('--today', type=date.fromisoformat, required=True)
    ack = sub.add_parser('ack-import')
    ack.add_argument('--receipt', type=Path, required=True)
    thanks = sub.add_parser('thanks')
    thanks.add_argument('--message-id', required=True)
    thanks.add_argument('--today', type=date.fromisoformat, required=True)
    args = p.parse_args()
    try:
        with Engine(args.state, json.loads(args.suppliers.read_text())) as e:
            if args.command in {'initial', 'reminders'}:
                result = getattr(e, args.command)(args.today)
            elif args.command == 'mark-sent':
                result = e.mark_sent(args.draft_id, args.message_id, args.sent_at)
            elif args.command == 'status':
                result = {'cycles':e.data['cycles'], 'messages':e.data['messages'], 'drafts':e.data['drafts'], 'event_count':len(e.data['events'])}
            elif args.command == 'receive':
                paths = ([args.input] if args.input.is_file() else sorted([*args.input.glob('new/*'), *args.input.glob('cur/*')]))
                result = []
                for path in paths:
                    if path.is_symlink() or path.stat().st_size > 16*1024*1024:
                        raise AutomationStop('input_symlink_or_size_limit')
                    result.append(e.receive(path.read_bytes(), args.today))
            elif args.command == 'retry':
                r = e.data['messages'][args.message_id]
                result = e.receive(e.read_checked(r['raw_path'], r['raw_hash']), args.today, args.token)
            elif args.command == 'ack-import':
                result = e.ack_import(json.loads(args.receipt.read_text()))
            else:
                result = e.thanks(args.message_id, args.today)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AutomationStop, ValueError, KeyError, OSError) as error:
        print(json.dumps({'status':'BLOCKED', 'reason':str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

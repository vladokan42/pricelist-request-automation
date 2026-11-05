"""Extracted request, calendar, thread and MIME core; see PROVENANCE.md."""
from __future__ import annotations
import email
import hashlib
import re

from dataclasses import dataclass
from datetime import date, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
TZ = timezone.utc
ALLOWED_SUFFIXES = {'.xls', '.xlsx'}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MIN_ATTACHMENT_BYTES = 64
@dataclass(frozen=True)
class Target:
    id: str
    supplier_name: str
    emails: tuple[str, ...]
    cycle_key: str
    reminder_count: int = 0

def _normalize_email(value):
    return parseaddr(str(value or ''))[1].strip().casefold()

class AutomationStop(RuntimeError):
    """Fail-closed operational stop with a stable reason code."""


def _decode_header(value: str | None) -> str:
    result: list[str] = []
    for part, charset in decode_header(value or ''):
        if isinstance(part, bytes):
            result.append(part.decode(charset or 'utf-8', errors='replace'))
        else:
            result.append(str(part))
    return ''.join(result)


def _message_id(value: str | None) -> str:
    value = str(value or '').strip()
    if not value:
        return ''
    if not (value.startswith('<') and value.endswith('>')):
        return ''
    if any(ch.isspace() for ch in value):
        return ''
    return value


def message_id_tokens(value: str | None) -> set[str]:
    return {f'<{token}>' for token in re.findall(r'<([^<>\s]+)>', str(value or ''))}


def _first_business_day_on_or_after_eighth(day: date) -> date:
    result = date(day.year, day.month, 8)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def _add_business_days(day: date, count: int) -> date:
    result = day
    while count:
        result += timedelta(days=1)
        if result.weekday() < 5:
            count -= 1
    return result


def _cycle_key(day: date) -> str:
    return day.strftime('%Y-%m')


def deterministic_message_id(target: Target, cycle: str, kind: str, reminder_number: int = 0) -> str:
    if kind not in {'initial', 'reminder'}:
        raise ValueError('invalid_draft_kind')
    if kind == 'initial' and reminder_number:
        raise ValueError('initial_must_not_have_reminder_number')
    if kind == 'reminder' and reminder_number not in {1, 2, 3}:
        raise ValueError('invalid_reminder_number')
    identity = f'{target.id}|{target.supplier_name}|{cycle}|{kind}|{reminder_number}'.encode('utf-8')
    return f'<pricelist-{hashlib.sha256(identity).hexdigest()[:32]}@automation.example>'


def _subject(day: date, supplier: str) -> str:
    short = re.sub(r'\s*\([^)]*\)', '', supplier).strip()
    return f'{day:%d.%m.%Y} запрос прайс-листа {short}'


def _body(kind: str, reminder_number: int) -> str:
    if kind == 'initial':
        return 'Добрый день.\nПрошу направить актуальный прайс-лист.'
    texts = {
        1: 'Добрый день.\nРанее запрашивал актуальный прайс-лист. Прошу направить его в ответном письме.',
        2: 'Добрый день.\nНапоминаю о запросе актуального прайс-листа. Прошу направить его в ответном письме.',
        3: 'Добрый день.\nТретий раз запрашиваю актуальный прайс-лист. Прошу направить его в ответном письме.',
    }
    return texts[reminder_number]


def _message_text(message: Message) -> str:
    parts = [part for part in message.walk() if part.get_content_type() in {'text/plain', 'text/html'}]
    for content_type in ('text/plain', 'text/html'):
        for part in parts:
            if part.get_content_type() != content_type:
                continue
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode(part.get_content_charset() or 'utf-8', errors='strict')
    return ''


def _message_day(message: Message) -> date:
    try:
        parsed = parsedate_to_datetime(message.get('Date') or '')
    except (ValueError, TypeError, IndexError) as error:
        raise AutomationStop('request_thread_date_invalid') from error
    if parsed.tzinfo is None:
        raise AutomationStop('request_thread_date_invalid')
    return parsed.astimezone(TZ).date()


def select_request_thread(rows: list[bytes], target: Target, day: date) -> list[bytes]:
    """Return one complete current-month request chain, oldest message first."""
    if target.cycle_key != _cycle_key(day):
        raise AutomationStop('request_thread_cycle_not_current')
    request_day = _first_business_day_on_or_after_eighth(day)
    expected_subject = _subject(request_day, target.supplier_name)
    parsed: dict[str, tuple[bytes, Message, list[str]]] = {}
    for raw in rows:
        message = email.message_from_bytes(raw)
        message_id = _message_id(message.get('Message-ID'))
        if not message_id or message_id in parsed:
            raise AutomationStop('request_thread_message_id_invalid_or_duplicate')
        if _normalize_email(message.get('To')) not in target.emails:
            raise AutomationStop('request_thread_recipient_invalid')
        message_day = _message_day(message)
        if not request_day <= message_day <= day:
            raise AutomationStop('request_thread_date_outside_current_cycle')
        subject = _decode_header(message.get('Subject')).strip()
        if subject not in {expected_subject, 'RE: ' + expected_subject}:
            raise AutomationStop('request_thread_subject_invalid')
        for name, value in (('X-Pricelist-Cycle', target.cycle_key), ('X-Pricelist-Supplier-ID', str(target.id))):
            if message.get_all(name) is not None and message.get_all(name) != [value]:
                raise AutomationStop(f'request_thread_marker_invalid:{name}')
        parsed[message_id] = (raw, message, re.findall(r'<[^<>\s]+>', str(message.get('References') or '')))
    roots = [message_id for message_id, (_raw, message, refs) in parsed.items()
             if not refs and not message_id_tokens(message.get('In-Reply-To'))]
    if len(roots) != 1:
        raise AutomationStop('request_thread_root_missing_or_ambiguous')
    root_id = roots[0]
    root_raw, root, _root_refs = parsed[root_id]
    if _message_day(root) < request_day or _decode_header(root.get('Subject')).strip() != expected_subject:
        raise AutomationStop('request_thread_root_invalid')
    if target.reminder_count not in range(4) or len(parsed) != target.reminder_count + 1:
        raise AutomationStop('request_thread_reminder_count_mismatch')
    ordered = [root_id]
    remaining = set(parsed).difference(ordered)
    while remaining:
        candidates = [message_id for message_id in remaining if parsed[message_id][2] == ordered]
        if len(candidates) != 1:
            raise AutomationStop('request_thread_incomplete_or_branched')
        message_id = candidates[0]
        direct = message_id_tokens(parsed[message_id][1].get('In-Reply-To'))
        if direct != {root_id}:
            raise AutomationStop('request_thread_parent_invalid')
        previous = parsed[message_id][1]
        number = len(ordered)
        number_headers = previous.get_all('X-Pricelist-Reminder-Number')
        deterministic_id = deterministic_message_id(target, target.cycle_key, 'reminder', number)
        if ((number_headers is not None and number_headers != [str(number)])
                or (number_headers is None and message_id != deterministic_id)
                or (message_id in {deterministic_message_id(target, target.cycle_key, 'reminder', n) for n in (1, 2, 3)}
                    and message_id != deterministic_id)):
            raise AutomationStop('request_thread_reminder_number_invalid')
        if (_decode_header(previous.get('Subject')).strip() != 'RE: ' + expected_subject
                or _message_day(previous) < _message_day(parsed[ordered[-1]][1])):
            raise AutomationStop('request_thread_reminder_subject_or_date_invalid')
        ordered.append(message_id)
        remaining.remove(message_id)
    return [parsed[message_id][0] for message_id in ordered]


def _attachment_filename(part: Message) -> str:
    raw = part.get_filename()
    if raw is None:
        return ''
    name = _decode_header(raw)
    if not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or ':' in name or '/' in name or '\\' in name or Path(name).name != name or len(name) > 180:
        raise AutomationStop('attachment_filename_unsafe')
    return name


def _validate_attachment(name: str, payload: bytes) -> None:
    from attachment_safety import inspect
    try:
        inspect(name, payload)
    except Exception as error:
        raise AutomationStop('attachment_rejected:' + type(error).__name__ + ':' + str(error)) from error


def extract_single_attachment(message: Message) -> tuple[str, bytes]:
    """Return one explicit file attachment, excluding inline signature resources."""
    found: list[tuple[str, bytes]] = []
    for part in message.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        # Outlook commonly serializes signature logos with both filename and
        # Content-Disposition: inline. They are not supplier documents.
        if part.get_content_disposition() != 'attachment':
            continue
        name = _attachment_filename(part)
        if not name:
            raise AutomationStop('attachment_without_filename')
        payload = part.get_payload(decode=True)
        if payload is None:
            raise AutomationStop('attachment_payload_missing')
        found.append((name, payload))
    if len(found) != 1:
        raise AutomationStop('expected_exactly_one_attachment')
    name, payload = found[0]
    _validate_attachment(name, payload)
    return name, payload

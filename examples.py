"""Generate only synthetic, genuine XLSX/XLS and MIME fixtures, without network."""
import io
import json
import struct
import zipfile
from datetime import date
from email import policy
from email.message import EmailMessage
from pathlib import Path

CONFIG = {'sender':'buyer@office.example', 'suppliers':[{'id':'demo', 'name':'Учебный поставщик', 'emails':['prices@supplier.example', 'backup@supplier.example']}]}


def xlsx():
    out = io.BytesIO()
    entries = {
        '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="root" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml':'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Пример" sheetId="1" r:id="sheet1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="sheet1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml':'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Товар</t></is></c><c r="B1" t="inlineStr"><is><t>Цена</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Учебный товар</t></is></c><c r="B2"><v>123.45</v></c></row></sheetData></worksheet>'}
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, value in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2026,1,1,0,0,0))
            z.writestr(info, value.encode())
    return out.getvalue()


def xls():
    # Minimal genuine BIFF8 workbook in a CFB v3 regular (not mini) stream.
    def rec(tag, data=b''):
        return struct.pack('<HH', tag, len(data)) + data
    def bof(kind):
        return rec(0x0809, struct.pack('<HHHHII', 0x0600, kind, 0x0DBB, 0x07CC, 0x41, 6))
    sheet_name = b'Demo'
    bound = lambda pos: rec(0x0085, struct.pack('<IBBB', pos, 0, 0, len(sheet_name)) + b'\0' + sheet_name)
    glob = bof(5) + rec(0x0042, struct.pack('<H',1200))
    prefix = glob + bound(0) + rec(0x000A)
    prefix = glob + bound(len(prefix)) + rec(0x000A)
    sheet = bof(0x10) + rec(0x0200, struct.pack('<IIHHH',0,1,0,1,0))
    sheet += rec(0x0203, struct.pack('<HHHd',0,0,0,123.45)) + rec(0x000A)
    stream = (prefix + sheet).ljust(4096,b'\0')
    free, end, fat = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    header = bytearray(512)
    header[:8] = bytes.fromhex('D0CF11E0A1B11AE1')
    struct.pack_into('<HHHHH',header,24,0x003E,3,0xFFFE,9,6)
    struct.pack_into('<IIIIIIIII',header,40,0,1,8,0,4096,end,0,end,0)
    struct.pack_into('<109I',header,76,9,*([free]*108))
    def entry(name, kind, child, start, size):
        out = bytearray(128)
        encoded = (name+'\0').encode('utf-16le')
        out[:len(encoded)] = encoded
        struct.pack_into('<HBBIII',out,64,len(encoded),kind,1,free,free,child)
        struct.pack_into('<IQ',out,116,start,size)
        return out
    directory = entry('Root Entry',5,1,end,0) + entry('Workbook',2,free,0,4096) + bytearray(256)
    allocation = list(range(1,8)) + [end,end,fat] + [free]*118
    return bytes(header) + stream + bytes(directory) + struct.pack('<128I',*allocation)


def reply(parent='<sent-request@office.example>', message_id='<reply@supplier.example>', sender='prices@supplier.example', filename='prices.xlsx', payload=None, when='Wed, 9 Sep 2026 10:00:00 +0000'):
    m = EmailMessage(policy=policy.SMTP)
    m['From'] = sender
    m['To'] = CONFIG['sender']
    m['Subject'] = 'RE: 08.09.2026 запрос прайс-листа Учебный поставщик'
    m['Message-ID'] = message_id
    m['Date'] = when
    m['In-Reply-To'] = parent
    m['References'] = parent
    m.set_content('Учебный ответ с прайсом.')
    m.add_attachment(xlsx() if payload is None else payload, maintype='application', subtype='octet-stream', filename=filename)
    m.set_boundary('synthetic-pricelist-fixture-boundary')
    return m.as_bytes()


def generate(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    (root/'suppliers.json').write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2))
    (root/'prices.xlsx').write_bytes(xlsx())
    (root/'prices.xls').write_bytes(xls())
    (root/'reply.eml').write_bytes(reply())
    (root/'reply-xls.eml').write_bytes(reply(message_id='<xls-reply@supplier.example>', filename='prices.xls', payload=xls()))
    return root


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('output', type=Path)
    print(generate(p.parse_args().output))

"""Bounded, non-executing container inspection. Not an antivirus."""
import io
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

LIMIT = 10 * 1024 * 1024
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def inspect(name, data):
    if not 64 <= len(data) <= LIMIT:
        raise ValueError('attachment_size_out_of_bounds')
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix == '.xlsx':
        inspect_xlsx(data)
    elif suffix == '.xls':
        inspect_xls(data)
    else:
        raise ValueError('attachment_suffix_not_allowed')


def inspect_xlsx(data):
    if not data.startswith(b'PK\x03\x04'):
        raise ValueError('xlsx_signature_invalid')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        if len(infos) > 256 or len(set(n.casefold() for n in names)) != len(names):
            raise ValueError('zip_entries_or_duplicates')
        if sum(i.file_size for i in infos) > 20 * 1024 * 1024:
            raise ValueError('zip_expansion_limit')
        for i in infos:
            n = i.filename.casefold()
            if ('\\' in n or n.startswith('/') or ':' in n or '..' in PurePosixPath(n).parts
                    or any(ord(c) < 32 for c in n) or (i.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('zip_path_unsafe')
            if i.flag_bits & 1 or i.file_size > 4 * 1024 * 1024 or i.file_size > max(1, i.compress_size)*100:
                raise ValueError('zip_encrypted_or_expansion_limit')
            if any(x in n for x in ('vba', 'macro', 'activex', 'embeddings', 'externallinks', 'connections', 'querytables', '_xmlsignatures')):
                raise ValueError('xlsx_active_or_signed_content_blocked')
            if not n.endswith(('.xml', '.rels', '/')):
                raise ValueError('xlsx_binary_part_blocked')
        required = {'[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml', 'xl/_rels/workbook.xml.rels'}
        if not required.issubset(names):
            raise ValueError('xlsx_required_parts_missing')
        trees = {}
        for i in infos:
            if i.is_dir():
                continue
            raw = z.read(i)
            decoded = raw.decode('utf-8-sig')
            if '\x00' in decoded or '<!DOCTYPE' in decoded.upper() or '<!ENTITY' in decoded.upper():
                raise ValueError('xml_entities_blocked')
            tree = ET.fromstring(raw)
            trees[i.filename] = tree
            for e in tree.iter():
                if e.tag.split('}')[-1] in {'f', 'externalReference', 'definedName', 'oleObject', 'control'}:
                    raise ValueError('xlsx_formula_or_active_content_blocked')
                if e.attrib.get('TargetMode', '').casefold() == 'external':
                    raise ValueError('xlsx_external_relationship_blocked')
                if e.tag.endswith('Relationship'):
                    target = e.attrib.get('Target', '')
                    if target.startswith(('/', '\\')) or '..' in PurePosixPath(target).parts or ':' in target:
                        raise ValueError('xlsx_relationship_path_unsafe')
                if any(x in str(v).casefold() for v in e.attrib.values() for x in ('macroenabled', 'vbaproject', 'macrosheet')):
                    raise ValueError('xlsx_macro_content_type_blocked')
        workbook = trees['xl/workbook.xml']
        sheets = workbook.findall(f'{{{NS}}}sheets/{{{NS}}}sheet')
        rels = {e.attrib.get('Id'): e.attrib.get('Target') for e in trees['xl/_rels/workbook.xml.rels']}
        if workbook.tag != f'{{{NS}}}workbook' or not sheets or len(sheets) > 32:
            raise ValueError('xlsx_workbook_invalid')
        for sheet in sheets:
            rid = sheet.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
            path = 'xl/' + str(rels.get(rid, ''))
            if path not in trees or trees[path].tag != f'{{{NS}}}worksheet':
                raise ValueError('xlsx_sheet_missing')
        if z.testzip() is not None:
            raise ValueError('xlsx_crc_invalid')


def inspect_xls(data):
    if not data.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
        raise ValueError('xls_signature_invalid')
    import olefile
    import xlrd
    with olefile.OleFileIO(io.BytesIO(data), raise_defects=olefile.DEFECT_INCORRECT) as ole:
        streams = ole.listdir(streams=True, storages=False)
        if not streams or len(streams) > 8:
            raise ValueError('xls_stream_limit')
        for path in ole.listdir(streams=True, storages=True):
            if len(path) != 1 or path[0] not in {'Workbook', 'Book', '\x05SummaryInformation', '\x05DocumentSummaryInformation'}:
                raise ValueError('xls_active_or_unknown_stream_blocked')
        book_names = [p for p in streams if p[0] in {'Workbook', 'Book'}]
        if len(book_names) != 1:
            raise ValueError('xls_workbook_missing')
        workbook = ole.openstream(book_names[0]).read()
        if len(workbook) > LIMIT:
            raise ValueError('xls_workbook_size_limit')
        offset = 0
        while offset + 4 <= len(workbook):
            tag, size = struct.unpack_from('<HH', workbook, offset)
            offset += 4
            if offset + size > len(workbook):
                raise ValueError('xls_record_truncated')
            record = workbook[offset:offset+size]
            # Formula, external book/name, encryption, VBA project records.
            if tag in {0x0006, 0x0206, 0x0406, 0x002F, 0x01AE, 0x0023, 0x00D3, 0x0018, 0x0017}:
                raise ValueError('xls_formula_external_or_macro_blocked')
            if tag == 0x0085 and len(record) >= 6 and record[5] != 0:
                raise ValueError('xls_nonworksheet_blocked')
            offset += size
    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    try:
        if not 1 <= book.nsheets <= 32:
            raise ValueError('xls_sheet_limit')
        for sheet in book.sheets():
            if sheet.nrows > 100000 or sheet.ncols > 1024:
                raise ValueError('xls_dimensions_limit')
    finally:
        book.release_resources()

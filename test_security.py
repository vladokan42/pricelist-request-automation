import io
import unittest
import zipfile
import core


def archive(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, value in entries:
            z.writestr(name, value)
    return out.getvalue()


class AttachmentRegression(unittest.TestCase):
    # Regression against the extracted original signature-only checks.
    def test_zip_is_not_workbook(self):
        with self.assertRaises(core.AutomationStop):
            core._validate_attachment('prices.xlsx', archive([('text.txt', 'x'*100)]))

    def test_zip_traversal(self):
        with self.assertRaises(core.AutomationStop):
            core._validate_attachment('prices.xlsx', archive([('../escape', 'x'*100)]))

    def test_case_insensitive_macro(self):
        with self.assertRaises(core.AutomationStop):
            core._validate_attachment('prices.xlsx', archive([('XL/VBAPROJECT.BIN', 'x'*100)]))

    def test_xls_signature_is_not_workbook(self):
        with self.assertRaises(core.AutomationStop):
            core._validate_attachment('prices.xls', bytes.fromhex('D0CF11E0A1B11AE1') + b'\0'*1024)

    def test_filename_controls(self):
        from email.message import EmailMessage
        m = EmailMessage()
        m.add_header('Content-Disposition', 'attachment', filename='bad\x01.xlsx')
        with self.assertRaises(core.AutomationStop):
            core._attachment_filename(m)

    def modified_xlsx(self, transform):
        import examples
        with zipfile.ZipFile(io.BytesIO(examples.xlsx())) as z:
            return archive(transform([(i.filename,z.read(i)) for i in z.infolist()]))

    def test_xlsx_formula_blocked(self):
        payload=self.modified_xlsx(lambda rows:[(n,v.replace(b'<v>123.45</v>',b'<f>1+1</f><v>2</v>')) for n,v in rows])
        with self.assertRaises(core.AutomationStop):
            core._validate_attachment('prices.xlsx',payload)

    def test_external_relationship_blocked(self):
        payload=self.modified_xlsx(lambda rows:[(n,v.replace(b'Target="worksheets/sheet1.xml"',b'Target="https://remote.example/book" TargetMode="External"')) for n,v in rows])
        with self.assertRaises(core.AutomationStop):
            core._validate_attachment('prices.xlsx',payload)

    def test_xml_entity_blocked(self):
        payload=self.modified_xlsx(lambda rows:[(n,b'<!DOCTYPE x [<!ENTITY q "hello">]>'+v if n=='xl/workbook.xml' else v) for n,v in rows])
        with self.assertRaises(core.AutomationStop):
            core._validate_attachment('prices.xlsx',payload)

    def test_zip_expansion_limit(self):
        with self.assertRaises(core.AutomationStop):
            core._validate_attachment('prices.xlsx',archive([('xl/padding.xml','x'*100000)]))

    def test_two_attachments_blocked(self):
        import examples
        from pricelist import parse
        m=parse(examples.reply())
        m.add_attachment(examples.xlsx(),maintype='application',subtype='octet-stream',filename='second.xlsx')
        with self.assertRaisesRegex(core.AutomationStop,'exactly_one'):
            core.extract_single_attachment(m)

    def test_size_and_suffix_limits(self):
        for name,data in [('prices.xlsm',b'x'*100),('prices.xlsx',b'x'*63),('prices.xlsx',b'x'*(10*1024*1024+1))]:
            with self.assertRaises(core.AutomationStop):
                core._validate_attachment(name,data)

    def test_xls_unknown_macro_stream_blocked(self):
        import examples
        raw=examples.xls().replace('Workbook'.encode('utf-16le'),'VBA12345'.encode('utf-16le'))
        with self.assertRaises(core.AutomationStop):
            core._validate_attachment('prices.xls',raw)


if __name__ == '__main__':
    unittest.main()

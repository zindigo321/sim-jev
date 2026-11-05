"""Offline signed-download export regressions using synthetic log strings."""
import json
from pathlib import Path
import unittest

from scripts.phase2d_audit import redact_download_payload


class DownloadRedactionTests(unittest.TestCase):
    def test_jsonl_decodes_escaped_query_separators_before_redacting(self):
        url = 'https://example.invalid/blob?X-Amz-Algorithm=example&X-Amz-Credential=synthetic-credential&X-Amz-Signature=synthetic-signature'
        original = json.dumps({'error': f'Get "{url}": failure', 'nested': [{'attempt': 1}]})
        payload = (original.replace('&', '\\u0026') + '\n').encode('utf-8')
        exported = redact_download_payload(Path('download.jsonl'), payload)
        row = json.loads(exported)
        self.assertEqual(row['error'], 'Get "https://example.invalid/blob?[download-signature-redacted]": failure')
        self.assertEqual(row['nested'], [{'attempt': 1}])
        self.assertNotIn(b'X-Amz-', exported)
        self.assertNotIn(b'synthetic-signature', exported)
        clean = b'{ "message": "no signed download", "count": 1 }\r\n'
        self.assertEqual(redact_download_payload(Path('clean.jsonl'), clean), clean)
        with self.assertRaisesRegex(ValueError, 'unredacted signed download parameter'):
            redact_download_payload(Path('unframed.log'), b'X-Amz-Signature=synthetic-value')

    def test_utf16_wrapped_query_is_removed_and_unrelated_bytes_are_preserved(self):
        original = 'before\r\nGet "https://example.invalid/blob?X-Amz-\r\nAlgorithm=example&X-Amz-Credential=synthetic-credential&X-Amz-\r\nSignature=synthetic-signature": failed\r\nafter\r\n'
        exported = redact_download_payload(Path('download.log'), original.encode('utf-16'))
        self.assertEqual(exported.decode('utf-8'), 'before\r\nGet "https://example.invalid/blob?[download-signature-redacted]": failed\r\nafter\r\n')
        self.assertNotIn(b'X-Amz-', exported)
        self.assertNotIn(b'synthetic-credential', exported)
        clean = 'unchanged UTF-16 log\r\n'.encode('utf-16')
        self.assertEqual(redact_download_payload(Path('clean.log'), clean), clean)
        json_payload = json.dumps({'nested': [original], 'count': 2}).encode('utf-8')
        parsed = json.loads(redact_download_payload(Path('download.json'), json_payload))
        self.assertEqual(parsed['nested'], [exported.decode('utf-8')])
        self.assertEqual(parsed['count'], 2)


if __name__ == '__main__':
    unittest.main()

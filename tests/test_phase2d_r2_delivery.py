import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from scripts.phase2d_r2_delivery import checked_entries,extract

class R2TransportTests(unittest.TestCase):
    def test_duplicate_and_traversal_are_rejected_before_output(self):
        for names in (['same','SAME'],['../escape'],['C:/escape'],['a\\b'],['a','a/b'],['a','a.'],['a','a '],['a/b','a/./b'],['a/b','a//b']):
            with self.subTest(names=names),tempfile.TemporaryDirectory() as t:
                root=Path(t);archive=root/'bad.zip'
                with zipfile.ZipFile(archive,'w') as z:
                    for n in names:z.writestr(n,b'x')
                if names==['a\\b']:
                    # Windows ZipInfo normalizes separators when writing. Alter
                    # both ZIP headers to exercise an actual hostile member.
                    archive.write_bytes(archive.read_bytes().replace(b'a/b',b'a\\b'))
                with self.assertRaises(ValueError):extract(archive,root/'output',root/'receipt.json')
                self.assertFalse((root/'output').exists())

    def test_hashes_and_real_empty_directory_survive(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);archive=root/'good.zip'
            import hashlib
            payload=b'unchanged\x00bytes'
            manifest={'files':[{'path':'payload','size_bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}],
                      'directories':['empty/branch-audit-store']}
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('payload',payload);z.writestr('empty/branch-audit-store/',b'')
                z.writestr('MANIFEST.json',json.dumps(manifest))
            result=extract(archive,root/'output',root/'receipt.json')
            self.assertEqual(result['files'],2)
            self.assertEqual((root/'output/payload').read_bytes(),payload)
            self.assertTrue((root/'output/empty/branch-audit-store').is_dir())
            self.assertEqual(json.loads((root/'receipt.json').read_text())['manifest_verified'],1)

    def test_changed_payload_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);archive=root/'bad.zip'
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('payload',b'wrong')
                z.writestr('MANIFEST.json',json.dumps({'files':[{'path':'payload','size_bytes':5,'sha256':'0'*64}]}))
            with self.assertRaisesRegex(ValueError,'manifest mismatch'):extract(archive,root/'output',root/'receipt.json')
            self.assertFalse((root/'receipt.json').exists())

if __name__=='__main__':unittest.main()

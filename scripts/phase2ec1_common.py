"""Durable, append-only accounting shared by every 2E-C1 process."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time
from sim_jev.provenance import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
PARENT = Path('G:/gpt-file/Codex/outputs/phase2e-b-20261110-141616.zip')
PARENT_SHA = 'ae10d491fa93dee3abf408f07f9905de5624a9adb92f213b010aa81fdb78c0aa'
A_SHA = '6d109b90d06fcf8b98537df01155a74d9588e90a75bfea095451bf346fdfae8d'
LIMITS = dict(reference=32768, online=4096, permutation=256, engineering_des=1024,
              trajectory=64, forward=360, smoke_update=40,
              P11_update=120, P29_update=120, S11_update=120, S29_update=120)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    with path.open('x', encoding='utf8') as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def append(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf8') as f:
        f.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n')
        f.flush()
        os.fsync(f.fileno())


def lines(path):
    if not Path(path).exists():
        return []
    with Path(path).open(encoding='utf8') as f:
        return [json.loads(row) for row in f if row.strip()]


def manifest(folder):
    folder = Path(folder)
    return [dict(path=p.relative_to(folder).as_posix(), bytes=p.stat().st_size, sha256=sha(p))
            for p in sorted(folder.rglob('*')) if p.is_file() and '__pycache__' not in p.parts]


def verify_files(root, rows):
    for row in rows:
        p = Path(root) / row['path']
        if sha(p) != row['sha256']:
            raise ValueError('sealed file changed: '+str(p))


class Journal:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'compute-journal.jsonl'
        self.entries = {}
        self.binding = sha(self.root / 'registration.json')
        for e in lines(self.path):
            if e['registration_sha256'] != self.binding:
                raise ValueError('ledger binding mismatch')
            previous = self.entries.get(e['key'])
            if (e['status'] == 'pending' and previous is not None or
                    e['status'] != 'pending' and (previous is None or previous['status'] != 'pending')):
                raise ValueError('ledger reservation/completion mismatch')
            self.entries[e['key']] = e
        self.counts = Counter(e['phase'] for e in self.entries.values())
        if any(self.counts[k] > v for k, v in LIMITS.items()):
            raise ValueError('compute budget exceeded')

    def reserve(self, phase, key):
        if phase not in LIMITS or self.counts[phase] >= LIMITS[phase] or key in self.entries:
            raise ValueError('budget exceeded or duplicate attempt: '+key)
        if phase == 'forward':
            used = sum(e.get('seconds', 60.) for e in self.entries.values() if e['phase'] == 'forward')
            if used + 60 > 3600:
                raise ValueError('forward seconds reservation would exceed 3600')
        e = dict(registration_sha256=self.binding, phase=phase, key=key, status='pending', unix=time.time())
        append(self.path, e)
        self.entries[key] = e
        self.counts[phase] += 1

    def finish(self, key, row, artifact=None, *, failed=False, seconds=None):
        if self.entries[key]['status'] != 'pending':
            raise ValueError('attempt already finished')
        e = dict(self.entries[key], status='failed' if failed else 'completed',
                 finished_unix=time.time(), scalar_sha256=canonical_sha256(row), artifact=artifact)
        if seconds is not None:
            e['seconds'] = seconds
        append(self.path, e)
        self.entries[key] = e

"""Pinned public assets for the 2E-C1 experiment; never uses credentials."""
from pathlib import Path
import hashlib
import json
import time

REPO = 'Qwen/Qwen3.5-0.8B-Base'
REVISION = 'dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68'
DEST = Path('G:/sim-jev-models/Qwen3.5-0.8B-Base-' + REVISION)


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def acquire():
    from huggingface_hub import HfApi, hf_hub_download
    info = HfApi(token=False).model_info(REPO, revision=REVISION, files_metadata=True)
    assert info.sha == REVISION
    names = [s for s in info.siblings if s.rfilename in
             ('LICENSE', 'README.md', 'config.json', 'merges.txt', 'vocab.json',
              'tokenizer.json', 'tokenizer_config.json', 'model.safetensors.index.json')
             or s.rfilename.endswith('.safetensors')]
    assert sum(s.size for s in names) < 4 * 1024 ** 3
    DEST.mkdir(parents=True, exist_ok=True)
    receipt = DEST / 'download-plan.json'
    plan = dict(repository=REPO, revision=REVISION, cap_bytes=4*1024**3,
                files=[dict(name=s.rfilename, bytes=s.size,
                            lfs_sha256=s.lfs.sha256 if s.lfs else None) for s in names])
    if receipt.exists():
        assert json.loads(receipt.read_text()) == plan
    else:
        receipt.write_text(json.dumps(plan, indent=2), encoding='utf8')
    rows = []
    for s in names:
        start = time.perf_counter()
        p = hf_hub_download(REPO, s.rfilename, revision=REVISION, local_dir=DEST, token=False)
        digest = sha(p)
        assert Path(p).stat().st_size == s.size
        if s.lfs:
            assert digest == s.lfs.sha256
        rows.append(dict(path=s.rfilename, bytes=s.size, sha256=digest,
                         seconds=time.perf_counter()-start))
        print('verified', s.rfilename, digest, flush=True)
    (DEST / 'asset-manifest.json').write_text(json.dumps(dict(repository=REPO,
        revision=REVISION, files=rows, credential_used=False, remote_code=False), indent=2), encoding='utf8')


if __name__ == '__main__':
    acquire()

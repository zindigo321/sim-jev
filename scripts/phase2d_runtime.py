"""Explicit, isolated Ollama acquisition/start/preflight. Never uses remote inference."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile

MODEL = 'qwen3:4b-instruct-2507-q4_K_M'
VERSION = '0.40.13'
ZIP_SHA256 = '7afa38624fa62ee5d8293422d0ab084c1bb4603134c7f4dafce11a033b57cc19'
RUNTIME = Path('G:/sim-jev-runtime/ollama') / VERSION
MODELS = Path('G:/sim-jev-models/ollama')

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def api(base, route, data=None, timeout=120):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + route, data=None if data is None else json.dumps(data).encode(),
                                 headers={'Content-Type':'application/json'})
    with opener.open(req, timeout=timeout) as r:
        return json.load(r)

def install(out):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    archive = RUNTIME.parent / (VERSION + '-windows-amd64.zip')
    url = f'https://github.com/ollama/ollama/releases/download/v{VERSION}/ollama-windows-amd64.zip'
    if not archive.exists():
        partial = archive.with_suffix('.partial')
        with urllib.request.urlopen(url, timeout=120) as src, partial.open('wb') as dst:
            total = 0
            while chunk := src.read(8 * 1024 * 1024):
                dst.write(chunk)
                total += len(chunk)
                print(f'downloaded {total} bytes', flush=True)
        partial.rename(archive)
    actual = sha(archive)
    if actual != ZIP_SHA256:
        raise ValueError('official release archive checksum mismatch')
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            dest = (RUNTIME / item.filename).resolve()
            if not dest.is_relative_to(RUNTIME.resolve()):
                raise ValueError('unsafe archive member')
        z.extractall(RUNTIME)
    save(out/'runtime-manifest.json', {'version':VERSION, 'url':url, 'archive_sha256':actual,
         'executable':str(RUNTIME/'ollama.exe'), 'executable_sha256':sha(RUNTIME/'ollama.exe')})

def start(out, port):
    exe = RUNTIME/'ollama.exe'
    if not exe.is_file():
        raise FileNotFoundError('run install first')
    with socket.socket() as s:
        s.bind(('127.0.0.1', port))
    MODELS.mkdir(parents=True, exist_ok=True)
    env = {k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP','TMP','PATH','USERPROFILE','LOCALAPPDATA','APPDATA') if k in os.environ}
    env.update(OLLAMA_MODELS=str(MODELS), OLLAMA_HOST=f'127.0.0.1:{port}', OLLAMA_NO_CLOUD='1',
               OLLAMA_NUM_PARALLEL='1', OLLAMA_KEEP_ALIVE='24h', OLLAMA_DEBUG='1')
    with (out/'server.stdout.log').open('ab') as stdout, (out/'server.stderr.log').open('ab') as stderr:
        proc = subprocess.Popen([str(exe), 'serve'], env=env, stdout=stdout, stderr=stderr,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    base = f'http://127.0.0.1:{port}'
    save(out/'server.json', {'pid':proc.pid, 'base_url':base, 'runtime':str(exe), 'models':str(MODELS),
                           'process_environment':{k:env[k] for k in env if k.startswith('OLLAMA_')}})
    for _ in range(60):
        try:
            print(api(base,'/api/version')); return
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError('owned Ollama server exited')
            time.sleep(.5)
    raise TimeoutError('server readiness timeout; inspect owned PID and logs')

def pull(out, base):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(urllib.request.Request(
        base+'/api/pull', data=json.dumps({'model':MODEL,'stream':True}).encode(),
        headers={'Content-Type':'application/json'}), timeout=120) as r, (out/'pull.jsonl').open('a',encoding='utf-8') as log:
        last = 0
        for line in r:
            log.write(line.decode()); log.flush()
            v=json.loads(line)
            if 'error' in v: raise RuntimeError(v['error'])
            if time.monotonic()-last>10 or v.get('status')=='success':
                print(v,flush=True); last=time.monotonic()
    preflight(out,base)

def download(out, base):
    """Acquire the exact public registry artifact using the desktop HTTP stack.

    Some desktop DNS configurations are incompatible with Ollama's downloader.
    This does not change the server's redirect rules, proxy, DNS, or model bytes.
    """
    root='https://registry.ollama.ai/v2/library/qwen3'
    with urllib.request.urlopen(root+'/manifests/4b-instruct-2507-q4_K_M',timeout=60) as r:
        raw=r.read()
    parsed=json.loads(raw)
    save(out/'registry-manifest.json',parsed)
    for item in [parsed['config'],*parsed['layers']]:
        digest=item['digest']
        if not digest.startswith('sha256:') or len(digest)!=71 or any(c not in '0123456789abcdef' for c in digest[7:]):
            raise ValueError('invalid registry digest')
        dest=MODELS/'blobs'/digest.replace(':','-'); dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.exists() and sha(dest)==digest[7:]: continue
        partial=dest.with_suffix('.download')
        with urllib.request.urlopen(root+'/blobs/'+digest,timeout=120) as src, partial.open('wb') as dst:
            total=0; last=0
            while chunk:=src.read(8*1024*1024):
                dst.write(chunk); total+=len(chunk)
                if time.monotonic()-last>10:
                    print(digest,total,flush=True);last=time.monotonic()
        if sha(partial)!=digest[7:] or partial.stat().st_size!=item['size']:
            raise ValueError('downloaded blob identity mismatch')
        partial.replace(dest)
    manifest=MODELS/'manifests/registry.ollama.ai/library/qwen3/4b-instruct-2507-q4_K_M'
    manifest.parent.mkdir(parents=True,exist_ok=True); manifest.write_bytes(raw)
    preflight(out,base)

def preflight(out,base):
    meta={r:api(base,'/api/'+r) for r in ('version','tags','ps')}
    meta['show']=api(base,'/api/show',{'model':MODEL,'verbose':True})
    manifest=MODELS/'manifests/registry.ollama.ai/library/qwen3/4b-instruct-2507-q4_K_M'
    raw=manifest.read_bytes(); parsed=json.loads(raw)
    blobs=[]
    for item in [parsed['config'],*parsed['layers']]:
        path=MODELS/'blobs'/item['digest'].replace(':','-')
        actual=sha(path)
        if item['digest']!='sha256:'+actual: raise ValueError('blob checksum mismatch')
        blobs.append({**item,'actual_sha256':actual,'path':str(path)})
    meta.update(model=MODEL,digest='sha256:'+hashlib.sha256(raw).hexdigest(),blobs=blobs,
                runtime_sha256=sha(RUNTIME/'ollama.exe'),tokenizer_source='embedded GGUF tokenizer metadata; upstream Qwen/Qwen3-4B-Instruct-2507')
    save(out/'model-manifest.json',meta)
    print(json.dumps({'model':MODEL,'digest':meta['digest'],'runtime':meta['version']},indent=2))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['install','start','pull','download','preflight'])
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--port',type=int,default=11439)
    a=p.parse_args(); a.output.mkdir(parents=True,exist_ok=True)
    t=time.monotonic(); record={'argv':sys.orig_argv,'action':a.action}
    try:
        if a.action=='install': install(a.output)
        elif a.action=='start': start(a.output,a.port)
        else: globals()[a.action](a.output,f'http://127.0.0.1:{a.port}')
        record['exit_code']=0
    except Exception as e:
        record.update(exit_code=1,error=f'{type(e).__name__}: {e}'); print(record['error'],file=sys.stderr)
    record['seconds']=time.monotonic()-t
    save(a.output/f'{a.action}-{time.time_ns()}.json',record)
    return record['exit_code']

if __name__=='__main__': raise SystemExit(main())

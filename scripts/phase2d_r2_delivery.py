"""Offline package transport: no project imports, network or environment changes."""
import argparse, hashlib, json, os, shutil, stat, time, zipfile
from pathlib import Path, PurePosixPath
PARENT_SHA='6acec98dcb5b9842a5639231b266047c6dc03d19f98fec758183fd95e15c0dcd'
def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def write(p,v):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x',encoding='utf-8') as f:json.dump(v,f,ensure_ascii=False,indent=2)
def checked_entries(z):
    seen=set(); entries=[]
    for i in z.infolist():
        s=i.orig_filename;p=PurePosixPath(s)
        if (not s or p.is_absolute() or '..' in p.parts or ':' in s or '\\' in s or s.startswith('/') or '\x00' in s
            or any(part.endswith((' ','.')) for part in p.parts)
            or s != p.as_posix()+('/' if i.is_dir() else '')):
            raise ValueError('unsafe archive path: '+s)
        key=p.as_posix().casefold()
        if key in seen: raise ValueError('duplicate archive path: '+s)
        if stat.S_ISLNK(i.external_attr>>16):raise ValueError('archive symlink: '+s)
        seen.add(key);entries.append(i)
    directory_keys={PurePosixPath(i.filename).as_posix().casefold() for i in entries if i.is_dir()}
    for i in entries:
        p=PurePosixPath(i.filename)
        for ancestor in p.parents:
            key=ancestor.as_posix().casefold()
            if key in seen and key not in directory_keys:
                raise ValueError('file/directory collision: '+i.filename)
    return entries
def extract(archive,destination,receipt,expected=None):
    archive=Path(archive);destination=Path(destination)
    if destination.exists():raise ValueError('extraction destination must be new')
    sha=digest(archive)
    if expected and sha!=expected:raise ValueError('archive SHA mismatch')
    start=time.perf_counter(); rows=[];directories=[]
    with zipfile.ZipFile(archive) as z:
        entries=checked_entries(z)
        manifest=json.loads(z.read('MANIFEST.json')) if 'MANIFEST.json' in z.namelist() else None
        expected_rows={r['path']:r for r in manifest['files']} if manifest else {}
        if manifest and len(expected_rows)!=len(manifest['files']):raise ValueError('duplicate manifest payload')
        if os.name=='nt' and any(len(str(destination.resolve().joinpath(*PurePosixPath(i.filename).parts)))>=250 for i in entries):
            raise ValueError('Windows extraction path too long; choose a new short destination (for example G:/r2p261104). No global setting changes are needed.')
        destination.mkdir(parents=True)
        for i in entries:
            p=destination.joinpath(*PurePosixPath(i.filename).parts)
            if not p.resolve().is_relative_to(destination.resolve()):raise ValueError('path escaped')
            if i.is_dir():p.mkdir(parents=True,exist_ok=True);directories.append(i.filename.rstrip('/'));continue
            p.parent.mkdir(parents=True,exist_ok=True)
            with z.open(i) as src,p.open('xb') as dst:shutil.copyfileobj(src,dst)
            row=dict(path=i.filename,size_bytes=p.stat().st_size,sha256=digest(p));rows.append(row)
            if i.filename in expected_rows:
                r=expected_rows[i.filename]
                if row['sha256']!=r['sha256'] or row['size_bytes']!=r.get('size_bytes',r.get('size')):raise ValueError('manifest mismatch '+i.filename)
        if set(expected_rows)-{r['path'] for r in rows}:raise ValueError('missing manifest payload')
        if manifest and 'directories' in manifest:
            for d in manifest['directories']:
                if not (destination/d).is_dir():raise ValueError('directory topology missing '+d)
    write(receipt,dict(status='passed',archive=str(archive),archive_sha256=sha,bytes=archive.stat().st_size,files=rows,directories=directories,manifest_verified=len(expected_rows),seconds=time.perf_counter()-start))
    return dict(status='passed',sha256=sha,files=len(rows),directories=len(directories))
def pack(source,reports,archive):
    source=Path(source);reports=Path(reports);archive=Path(archive)
    if archive.exists():raise ValueError('archive already exists')
    entries=[];dirs=set()
    for folder,prefix in ((source,'source'),(reports,'reports')):
        for current,ds,ns in os.walk(folder):
            ds[:]=sorted(d for d in ds if d not in ('.git','__pycache__','runs','audit-record','parent-evidence','final-extracted','package-staging','review-evidence'))
            rel=Path(current).relative_to(folder)
            dirs.add((Path(prefix)/rel).as_posix())
            for n in sorted(ns):
                p=Path(current)/n
                if p.is_symlink() or p.suffix in ('.pyc','.pyo'):continue
                if prefix=='source' and rel==Path('.') and n not in ('.gitignore','README.md','pyproject.toml'):continue
                entries.append((p,(Path(prefix)/rel/n).as_posix()))
    manifest=dict(schema='phase2d-r2-dependent-package-v1',parent_sha256=PARENT_SHA,external_dependencies='source/docs/PHASE2D_R2_COMMANDS.md',files=[],directories=sorted(dirs))
    archive.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for p,arc in sorted(entries,key=lambda r:r[1]):
            b=p.read_bytes();z.writestr(arc,b)
            manifest['files'].append(dict(path=arc,size_bytes=len(b),sha256=hashlib.sha256(b).hexdigest()))
        for d in sorted(dirs):z.writestr(d+'/',b'')
        z.writestr('MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    return dict(archive=str(archive),sha256=digest(archive),files=len(entries),directories=len(dirs))
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['extract','parent','pack']);p.add_argument('--archive',type=Path,required=True);p.add_argument('--destination',type=Path);p.add_argument('--receipt',type=Path);p.add_argument('--source',type=Path);p.add_argument('--reports',type=Path);a=p.parse_args()
    print(json.dumps(pack(a.source,a.reports,a.archive) if a.action=='pack' else extract(a.archive,a.destination,a.receipt,PARENT_SHA if a.action=='parent' else None)))
if __name__=='__main__':main()

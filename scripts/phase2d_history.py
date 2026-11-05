"""Explicit, content-bound compatibility for the immutable R1 scientific batch.

This module verifies historical source bytes from the declared parent archive.
It never imports or executes those old source files. R2 executes all validators,
renderers, reconstruction and simulation code from the current project.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path, PurePosixPath
import stat
import zipfile

from sim_jev.checkpoint import _loads
from sim_jev.local_provider import validate_identity
from sim_jev.provenance import canonical_sha256, code_fingerprint

ROOT=Path(__file__).resolve().parents[1]
COMPATIBILITY_VERSION='phase2d-r2-historical-r1-compat-v1'
PARENT_SHA256='6acec98dcb5b9842a5639231b266047c6dc03d19f98fec758183fd95e15c0dcd'
SCIENTIFIC_SOURCE_SHA256='88a245221a079462ee8d079d6585aa573f0924ce79a4cc2861255e3dc802af06'
FREEZE_SHA256='83567c1fb765ebd1180edf24d07035e220b79e8a141a237229c6f4a53353e91d'
BATCH_MEMBER='project/runs/phase2d-r1-20261103-191950/'
OLD_RENDERER='lossless-tables-and-shared-json-scores-v2'


def file_sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def checked_zip_entries(bundle):
    """Reject ambiguous, escaping, Windows-alias and symlink archive members."""
    seen=set()
    for item in bundle.infolist():
        name=item.orig_filename;path=PurePosixPath(name)
        parts=path.parts;normalized=path.as_posix()
        canonical_name=normalized+('/' if name.endswith('/') else '')
        key=normalized.casefold()
        if (not name or not parts or name!=canonical_name or name!=item.filename or
            path.is_absolute() or '..' in parts or '\\' in name or ':' in name or
            any(part in ('.','') or part.endswith((' ','.')) for part in parts) or
            key in seen or stat.S_ISLNK(item.external_attr>>16)):
            raise ValueError('unsafe or duplicate archive member: '+name)
        seen.add(key)
    return seen


class HistoricalR1Binding:
    """Verified frozen identity and source provenance; no fingerprint substitution."""
    def __init__(self,evidence_root,parent_archive,*,version=COMPATIBILITY_VERSION):
        if version!=COMPATIBILITY_VERSION:raise ValueError('unknown historical compatibility version')
        self.root=Path(evidence_root).resolve();self.archive=Path(parent_archive).resolve()
        self.version=version
        if file_sha256(self.archive)!=PARENT_SHA256:raise ValueError('R1 parent archive SHA256 mismatch')
        with zipfile.ZipFile(self.archive) as bundle:
            checked_zip_entries(bundle)
            historical=_loads(bundle.read(BATCH_MEMBER+'freeze.json').decode('utf-8'))
            if canonical_sha256(historical)!=FREEZE_SHA256:raise ValueError('unknown frozen R1 experiment')
            source=historical['source']
            if (source['sha256']!=SCIENTIFIC_SOURCE_SHA256 or
                canonical_sha256(source['files'])!=SCIENTIFIC_SOURCE_SHA256):
                raise ValueError('historical scientific source manifest identity mismatch')
            paths=set()
            for row in source['files']:
                relative=PurePosixPath(row['path'])
                if relative.is_absolute() or '..' in relative.parts or row['path'] in paths:
                    raise ValueError('invalid historical scientific source path')
                paths.add(row['path']);payload=bundle.read('project/'+row['path'])
                if len(payload)!=row['size_bytes'] or hashlib.sha256(payload).hexdigest()!=row['sha256']:
                    raise ValueError('historical scientific source bytes mismatch: '+row['path'])
        self._frozen=historical
        self.frozen_for(self.root)

    def frozen_for(self,root):
        if Path(root).resolve()!=self.root:raise ValueError('historical binding belongs to different evidence root')
        frozen=_loads((self.root/'freeze.json').read_text(encoding='utf-8'))
        seal=_loads((self.root/'freeze-sha256.json').read_text(encoding='utf-8'))
        if frozen!=self._frozen or canonical_sha256(frozen)!=FREEZE_SHA256 or seal!={'canonical_sha256':FREEZE_SHA256}:
            raise ValueError('historical freeze differs from pinned parent experiment')
        validate_identity(frozen['identity'])
        if (frozen['identity']['renderer']!=OLD_RENDERER or
            frozen['identity']['protocol']!='local-preference-scores-v2' or not frozen['formal_live_allowed']):
            raise ValueError('unsupported historical renderer/protocol or locked experiment')
        return deepcopy(frozen)

    def identity(self):
        auditor_files=('scripts/phase2d_r2.py','scripts/phase2d_history.py','scripts/phase2d_r1_data_audit.py')
        auditor={name:file_sha256(ROOT/name) for name in auditor_files}
        return {'compatibility_version':self.version,'parent_archive_sha256':PARENT_SHA256,
            'scientific_source_sha256':SCIENTIFIC_SOURCE_SHA256,'freeze_canonical_sha256':FREEZE_SHA256,
            'historical_source_files_verified':len(self._frozen['source']['files']),
            'execution_source_sha256':code_fingerprint(ROOT)['sha256'],
            'auditor_files':auditor,'auditor_sha256':canonical_sha256(auditor),
            'auditor_scope':'explicit R2 entry, historical binding and strict data auditor files v1',
            'legacy_reconstruction':'exact public values, arrays and canonical hashes before factory object traversal',
            'historical_sources_executed':False}

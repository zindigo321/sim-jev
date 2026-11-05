"""Phase 2E-A local-only campaign, durable quotas and real development gate.

No acquisition/install path is provided. All output is evidence, never source.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Thread
import time
import traceback
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from sim_jev.jev import _atomic_json
from sim_jev.local_provider import LocalProvider, LocalError, validate_record, render_for_identity
from sim_jev.provenance import code_fingerprint, canonical_sha256
from scripts.phase2d_local import unpack
from scripts.phase2d_r1 import immutable
from scripts.phase2d_r2_delivery import digest

LIMITS = {'development': 40, 'diagnostics': 80}
TIME_LIMITS = {'development': 1800., 'diagnostics': 5400.}
RUNTIME = Path('G:/sim-jev-runtime/ollama/0.40.13')
MODELS = Path('G:/sim-jev-models/ollama')
TOKEN_PY = Path('G:/sim-jev-runtime/tokenizer-env/Scripts/python.exe')
TOKEN_JSON = Path('G:/sim-jev-models/tokenizer/cdbee75f17c01a7cc42f958dc650907174af0554/tokenizer.json')
PARENT = Path('G:/sim-jev-audit/sim-jev-phase2d-r1-20261103-191950-02.zip')
PARENT_SHA = '6acec98dcb5b9842a5639231b266047c6dc03d19f98fec758183fd95e15c0dcd'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    _atomic_json(Path(path), value)


class CampaignBudget:
    """Single campaign identity, phase deadlines, and fail-closed reservations."""
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        v = read(self.path)
        if (v['limits'] != LIMITS or v['phase_max_seconds'] != TIME_LIMITS or
                v['max_seconds'] != 7200 or v['max_attempts'] != 120):
            raise LocalError('campaign_budget_identity_mismatch')
        if (v.get('protocol') != 'phase2e-durable-generation-v1' or
                type(v.get('campaign_id')) is not str or not v['campaign_id'] or
                type(v.get('attempts')) is not list or len(v['attempts']) > 120):
            raise LocalError('invalid_campaign_budget')
        seen = set()
        for attempt in v['attempts']:
            aid = attempt.get('attempt_id')
            if (type(aid) is not str or len(aid) != 32 or aid in seen or
                    attempt.get('phase') not in LIMITS or
                    attempt.get('status') not in ('pending', 'completed', 'uncertain')):
                raise LocalError('invalid_campaign_attempt')
            seen.add(aid)
            seconds = attempt.get('seconds', 0.)
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                raise LocalError('invalid_campaign_duration')
        if (type(v.get('seconds')) not in (int, float) or not math.isfinite(v['seconds']) or
                v['seconds'] != sum(a.get('seconds', 0.) for a in v['attempts'])):
            raise LocalError('campaign_time_accounting_mismatch')
        return v

    def reserve(self, phase, key, *, logical_request_id=None, attempt_index=None):
        v = self.read()
        if any(a['status'] in ('pending', 'uncertain') for a in v['attempts']):
            raise LocalError('uncertain_prior_generation')
        if phase not in LIMITS:
            raise LocalError('unknown_generation_phase')
        used = [a for a in v['attempts'] if a['phase'] == phase]
        seconds = sum(a.get('seconds', 0.) for a in used)
        left = min(TIME_LIMITS[phase] - seconds, 7200 - v['seconds'])
        if len(used) >= LIMITS[phase] or len(v['attempts']) >= 120 or left <= 0:
            raise LocalError('local_budget_exhausted')
        previous = [a for a in v['attempts'] if a['logical_request_id'] == logical_request_id]
        if (type(logical_request_id) is not str or not logical_request_id or
                type(attempt_index) is not int or attempt_index not in (0, 1) or
                len(previous) != attempt_index or
                any(a['phase'] != phase or a['key'] != key for a in previous)):
            raise LocalError('logical_request_attempt_limit')
        timeout = min(120., left)
        v['attempts'].append(dict(phase=phase, key=key, status='pending',
            started_unix=time.time(), attempt_id=uuid.uuid4().hex,
            logical_request_id=logical_request_id, attempt_index=attempt_index,
            reserved_deadline_seconds=timeout))
        save(self.path, v)
        return len(v['attempts']) - 1, timeout

    def finish(self, index, seconds, status, *, artifact=None):
        v = self.read()
        if status not in ('completed', 'uncertain') or v['attempts'][index]['status'] != 'pending':
            raise LocalError('invalid_generation_transition')
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError('invalid duration')
        v['attempts'][index].update(seconds=seconds, status=status)
        if artifact is not None:
            v['attempts'][index]['artifact'] = deepcopy(artifact)
        v['seconds'] += seconds
        save(self.path, v)


class HttpLedger:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'http-budget.json'

    def reserve(self, route, data):
        v = read(self.path)
        if (v.get('protocol') != 'phase2e-local-http-v1' or v.get('max_metadata') != 400 or
                type(v.get('requests')) is not list):
            raise LocalError('http_budget_identity_mismatch')
        if any(r['status'] in ('pending', 'uncertain') for r in v['requests']):
            raise LocalError('uncertain_prior_http')
        kind = 'generation' if route == '/api/generate' else 'metadata'
        if kind == 'metadata' and sum(r['kind'] == kind for r in v['requests']) >= 400:
            raise LocalError('metadata_budget_exhausted')
        if route not in ('/api/generate', '/api/tags', '/api/version', '/api/ps', '/api/show'):
            raise LocalError('unapproved_local_route')
        reservation = None
        if kind == 'generation':
            campaign = CampaignBudget(self.root / 'generation-budget.json').read()
            pending = [a for a in campaign['attempts'] if a['status'] == 'pending']
            if len(pending) != 1:
                raise LocalError('generation_http_without_reservation')
            reservation = pending[0]
        index = len(v['requests'])
        v['requests'].append(dict(index=index, route=route, kind=kind, status='pending',
            request_sha256=canonical_sha256(data), started_unix=time.time(),
            generation_attempt_id=reservation['attempt_id'] if reservation else None))
        save(self.path, v)
        return index

    def finish(self, index, response, seconds, *, error=None):
        v = read(self.path)
        if v['requests'][index]['status'] != 'pending':
            raise LocalError('invalid_http_transition')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError('invalid HTTP duration')
        artifact = dict(index=index, response=response, error=error, seconds=seconds)
        path = self.root / 'http' / f'{index:04d}-response.json'
        immutable(path, artifact)
        v['requests'][index].update(status='uncertain' if error else 'completed', seconds=seconds,
            artifact=path.relative_to(self.root).as_posix(), artifact_sha256=digest(path))
        save(self.path, v)


def audit_http(root):
    """Offline reconcile every real HTTP reservation with its raw artifacts."""
    root = Path(root)
    ledger = read(root / 'http-budget.json')
    if ledger.get('protocol') != 'phase2e-local-http-v1' or ledger.get('max_metadata') != 400:
        raise ValueError('HTTP ledger identity differs')
    campaign = CampaignBudget(root / 'generation-budget.json').read()
    by_attempt = {a['attempt_id']:a for a in campaign['attempts']}
    generation_ids = set()
    pending = metadata = generations = 0
    for index, row in enumerate(ledger['requests']):
        kind = 'generation' if row['route'] == '/api/generate' else 'metadata'
        if (row['index'] != index or row['kind'] != kind or
                row['route'] not in ('/api/generate','/api/tags','/api/version','/api/ps','/api/show')):
            raise ValueError('HTTP ledger route/index differs')
        request = read(root / 'http' / f'{index:04d}-request.json')
        url = urlsplit(request['base_url'])
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password or
                url.path or url.query or url.fragment or request['route'] != row['route'] or
                canonical_sha256(request['data']) != row['request_sha256']):
            raise ValueError('HTTP request identity differs or is not literal loopback')
        if kind == 'generation':
            generations += 1
            aid = row.get('generation_attempt_id')
            if aid not in by_attempt or aid in generation_ids:
                raise ValueError('HTTP generation reservation differs or reused')
            generation_ids.add(aid)
        else:
            metadata += 1
        if row['status'] in ('pending','uncertain'):
            pending += 1
        elif row['status'] != 'completed':
            raise ValueError('unknown HTTP reservation state')
        if row['status'] != 'pending':
            path = root / row['artifact']
            if (not path.resolve().is_relative_to((root/'http').resolve()) or
                    digest(path) != row['artifact_sha256']):
                raise ValueError('HTTP response artifact changed')
            response = read(path)
            if (response['index'] != index or response['seconds'] != row['seconds'] or
                    bool(response['error']) != (row['status'] == 'uncertain')):
                raise ValueError('HTTP response transition differs')
    if metadata > 400 or generations > 120:
        raise ValueError('HTTP request cap exceeded')
    if any(a['status'] == 'completed' and a['attempt_id'] not in generation_ids for a in campaign['attempts']):
        raise ValueError('completed generation has no recorded HTTP request')
    return dict(status='passed' if pending == 0 else 'partial', actual_http_requests=len(ledger['requests']),
        metadata_requests=metadata, generation_requests=generations, pending_uncertain=pending)


def audit_generation_record(root, record, phase, store, gpu_receipt, seen_ids):
    """Bind an already strictly parsed live record to budget, raw HTTP and GPU."""
    root, store = Path(root), Path(store)
    budget = CampaignBudget(root / 'generation-budget.json').read()
    http_rows = read(root / 'http-budget.json')['requests']
    ident = read(root / 'freeze.json')['identity']
    if record['evidence'] != 'local_inference' or record['valid_local_model'] is not True:
        raise ValueError('old cache or fixture cannot count as a new generation')
    if (gpu_receipt['cache_key'] != record['cache_key'] or gpu_receipt['phase'] != phase or
            gpu_receipt['evidence'] != 'local_inference' or
            len(gpu_receipt['tokenizer']) != len(record['attempts'])):
        raise ValueError('GPU/token receipt identity differs')
    audit_gpu_receipt(root, gpu_receipt, ident)
    gpu = gpu_receipt['gpu']
    loaded = next((x for x in gpu['models'] if x['name'] == ident['model']), None)
    if (loaded is None or loaded['size_vram'] <= 0 or loaded['context_length'] != 16384 or
            loaded['digest'].removeprefix('sha256:') != ident['model_digest'].removeprefix('sha256:')):
        raise ValueError('GPU evidence invalid')
    for index, attempt in enumerate(record['attempts']):
        aid = attempt['attempt_id']
        if aid in seen_ids:
            raise ValueError('reused generation attempt')
        seen_ids.add(aid)
        reservation = next(a for a in budget['attempts'] if a['attempt_id'] == aid)
        if (reservation['phase'] != phase or reservation['key'] != record['cache_key'] or
                reservation['status'] != 'completed' or reservation['attempt_index'] != index or
                reservation['logical_request_id'] != record['logical_request_id'] or
                reservation['logical_request_id'] != attempt['logical_request_id'] or
                reservation['seconds'] != attempt['seconds']):
            raise ValueError('generation reservation mismatch')
        path = store / reservation['artifact']['path']
        if path.name != 'attempt-'+aid+'.json' or path.resolve().parent != store.resolve():
            raise ValueError('generation attempt artifact path differs')
        evidence = read(path)
        expected = dict(identity=ident, cache_key=record['cache_key'], rendered=record['rendered'],
            attempt={k:v for k,v in attempt.items() if k != 'format_error'})
        if (canonical_sha256(evidence) != reservation['artifact']['sha256'] or
                canonical_sha256(evidence) != canonical_sha256(expected)):
            raise ValueError('raw attempt artifact changed or differs from completion')
        token = gpu_receipt['tokenizer'][index]
        if (token['prompt_sha256'] != hashlib.sha256(attempt['body']['prompt'].encode()).hexdigest() or
                type(token['tokens']) is not int or token['tokens'] != attempt['independent_prompt_tokens']):
            raise ValueError('independent token receipt differs')
        requests = [r for r in http_rows if r.get('generation_attempt_id') == aid]
        if len(requests) != 1 or requests[0]['status'] != 'completed':
            raise ValueError('raw generation HTTP missing or uncertain')
        row = requests[0]
        request = read(root / 'http' / f"{row['index']:04d}-request.json")
        response = read(root / row['artifact'])
        if (canonical_sha256(request['data']) != canonical_sha256(attempt['body']) or
                canonical_sha256(response['response']) != canonical_sha256(attempt['raw'])):
            raise ValueError('raw generation HTTP differs from model attempt')
    return [a['attempt_id'] for a in record['attempts']]


def audit_gpu_receipt(root, receipt, ident):
    root = Path(root)
    index = receipt.get('gpu_http_index')
    rows = read(root/'http-budget.json')['requests']
    if type(index) is not int or not 0 <= index < len(rows):
        raise ValueError('GPU metadata HTTP binding missing')
    row = rows[index]
    if row['route'] != '/api/ps' or row['status'] != 'completed':
        raise ValueError('GPU metadata HTTP missing or uncertain')
    response = read(root/row['artifact'])
    if canonical_sha256(response['response']) != canonical_sha256(receipt['gpu']):
        raise ValueError('GPU receipt differs from raw metadata HTTP')
    loaded = next((x for x in receipt['gpu']['models'] if x['name'] == ident['model']), None)
    if (loaded is None or loaded['size_vram'] <= 0 or loaded['context_length'] != 16384 or
            loaded['digest'].removeprefix('sha256:') != ident['model_digest'].removeprefix('sha256:')):
        raise ValueError('GPU evidence invalid')


def audit_diagnostic_generations(root, shared_records):
    """Reconcile shared scores once per input; replays create no new attempts."""
    root = Path(root)
    receipts = read(root/'diagnostics/responses/ranking-receipts.json')
    if len(receipts) != len(shared_records):
        raise ValueError('diagnostic GPU/ranking receipt count differs')
    seen, live_keys = set(), {}
    counts = dict(local_inference=0, local_replay=0, deterministic=0)
    for record, receipt in zip(shared_records, receipts):
        if (receipt['cache_key'] != record['cache_key'] or receipt['phase'] != 'diagnostics' or
                receipt['evidence'] != record['evidence']):
            raise ValueError('diagnostic shared score/GPU receipt identity differs')
        evidence = record['evidence']
        if evidence not in counts:
            raise ValueError('diagnostic scoring is not real, replay or deterministic')
        counts[evidence] += 1
        if evidence == 'local_inference':
            if record['cache_key'] in live_keys:
                raise ValueError('duplicate exact cache key counted as new generation')
            audit_generation_record(root, record, 'diagnostics', root/'diagnostics/responses', receipt, seen)
            live_keys[record['cache_key']] = record
        elif evidence == 'local_replay':
            source = live_keys.get(record['cache_key'])
            if source is None or receipt['tokenizer']:
                raise ValueError('diagnostic replay has no earlier same-batch generation')
            audit_gpu_receipt(root, receipt, read(root/'freeze.json')['identity'])
            for field in ('identity','rendered','ranked_ids','choice','preference_scores','ties','attempts'):
                if canonical_sha256(source[field]) != canonical_sha256(record[field]):
                    raise ValueError('diagnostic replay differs from original score')
            if record['local_generation_attempts'] or record['actual_input_tokens'] or record['actual_output_tokens']:
                raise ValueError('diagnostic replay charged as new generation')
        elif receipt['gpu'] is not None or receipt['tokenizer'] or record['attempts']:
            raise ValueError('deterministic skip has generation/GPU artifacts')
    budget = CampaignBudget(root/'generation-budget.json').read()
    attempts = [a for a in budget['attempts'] if a['phase'] == 'diagnostics']
    seconds = sum(a.get('seconds',0.) for a in attempts)
    if (seen != {a['attempt_id'] for a in attempts} or any(a['status'] != 'completed' for a in attempts) or
            len(attempts) > 80 or seconds > 5400 or budget['seconds'] > 7200 or
            any(a.get('seconds',0.) > a['reserved_deadline_seconds'] for a in attempts)):
        raise ValueError('diagnostic generation budget incomplete or exceeded')
    http = audit_http(root)
    if http['status'] != 'passed':
        raise ValueError('diagnostic HTTP pending or uncertain')
    return dict(status='passed', shared_records=len(shared_records), **counts,
        actual_attempts=len(attempts), generation_seconds=seconds, http=http)


class TokenCounter:
    def __init__(self, python, asset):
        self.python, self.asset = str(python), str(asset)
        self.calls = []

    def __call__(self, prompt):
        t = time.perf_counter()
        code = ('import json,sys,socket;from unittest.mock import patch;from tokenizers import Tokenizer;'
                'p=json.load(sys.stdin);'
                't=Tokenizer.from_file(sys.argv[1]);print(len(t.encode(p,add_special_tokens=False).ids))')
        proc = subprocess.run([self.python, '-c', code, self.asset], input=json.dumps(prompt),
            capture_output=True, text=True, encoding='utf-8', timeout=60, check=True)
        n = int(proc.stdout.strip())
        self.calls.append(dict(prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                               tokens=n, seconds=time.perf_counter() - t))
        return n


class AuditedProvider(LocalProvider):
    def __init__(self, root, store, phase, *, base_url, counter):
        self.root = Path(root)
        self.http_ledger = HttpLedger(root)
        self.token_times = counter
        self.ranking_receipts = []
        self.ranking_persistence_receipts = []
        super().__init__(read(self.root / 'freeze.json')['identity'], store, mode='live',
            base_url=base_url, budget=CampaignBudget(self.root / 'generation-budget.json'),
            phase=phase, token_counter=counter)

    def _http(self, route, data=None, timeout=120):
        index = self.http_ledger.reserve(route, data)
        immutable(self.root / 'http' / f'{index:04d}-request.json',
            dict(route=route, data=data, base_url=self.base_url, timeout=timeout))
        t = time.perf_counter()
        try:
            response = super()._http(route, data, timeout)
        except Exception as e:
            self.http_ledger.finish(index, None, time.perf_counter() - t,
                                    error=f'{type(e).__name__}: {e}')
            raise
        self.http_ledger.finish(index, response, time.perf_counter() - t)
        return response

    def rank(self, legal, pool, *, force=False):
        t = time.perf_counter()
        n = len(self.token_times.calls)
        record = super().rank(legal, pool, force=force)
        validation_started = time.perf_counter()
        validate_record(record, legal, pool, self.identity)
        validation_seconds = time.perf_counter() - validation_started
        receipt = dict(cache_key=record['cache_key'], phase=self.phase,
            evidence=record['evidence'], seconds=time.perf_counter() - t,
            tokenizer=self.token_times.calls[n:], gpu=None, gpu_http_index=None,
            render_seconds=self.last_render_seconds, strict_validation_seconds=validation_seconds,
            timing_scope='render_seconds measures the actual initial provider render; strict validation includes its own verification render')
        if len(pool) >= 2:
            metadata = self._http('/api/ps')
            loaded = next((x for x in metadata['models'] if x['name'] == self.identity['model']), None)
            if (loaded is None or loaded['size_vram'] <= 0 or loaded['context_length'] != 16384 or
                loaded['digest'].removeprefix('sha256:') != self.identity['model_digest'].removeprefix('sha256:')):
                raise LocalError('gpu_context_or_digest_mismatch')
            receipt['gpu'] = metadata
            receipt['gpu_http_index'] = len(read(self.root/'http-budget.json')['requests'])-1
        self.ranking_receipts.append(receipt)
        persistence_started = time.perf_counter()
        save(self.store / 'ranking-receipts.json', self.ranking_receipts)
        self.ranking_persistence_receipts.append(dict(index=len(self.ranking_receipts)-1,
            cache_key=record['cache_key'], phase=self.phase,
            serialization_and_atomic_persistence_seconds=time.perf_counter()-persistence_started,
            scope='actual ranking-receipts.json serialization and atomic persistence only; excludes this companion file write'))
        save(self.store / 'ranking-persistence-times.json', self.ranking_persistence_receipts)
        return record


def check_freeze(root):
    frozen = read(Path(root) / 'freeze.json')
    if frozen['source'] != code_fingerprint(ROOT):
        raise ValueError('execution source changed: preserve this batch and create a new batch')
    return frozen


def prepare(root, *, legacy, tokenizer_python=TOKEN_PY, tokenizer_json=TOKEN_JSON):
    """Offline only, after full tests; every manifest precedes new runtime I/O."""
    from sim_jev.local_provider import identity_v4, REVERSIBLE_RENDERER
    root, legacy = Path(root), Path(legacy)
    tests = read(root / 'engineering-tests/command.json')
    current = code_fingerprint(ROOT)
    if (tests['status'] != 'passed' or tests['source'] != current or not tests['source_unchanged'] or
            tests['skipped'] or tests['failed'] or tests['errors']):
        raise ValueError('full original and new test gate not passed on this source')
    old = read(legacy / 'development-identity.json')
    if digest(tokenizer_python) != old['tokenizer_runtime']['python_executable_sha256']:
        raise ValueError('tokenizer executable mismatch')
    if digest(tokenizer_json) != old['tokenizer_sha256']:
        raise ValueError('tokenizer asset mismatch')
    info = subprocess.run([str(tokenizer_python), '-c',
        'import platform,tokenizers,json;print(json.dumps({"python":platform.python_version(),"tokenizers":tokenizers.__version__}))'],
        capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
    versions = json.loads(info.stdout)
    if any(versions[k] != old['tokenizer_runtime'][k] for k in versions):
        raise ValueError('tokenizer versions mismatch')
    manifest = read(legacy / 'hardware/model-manifest.json')
    ident = identity_v4(manifest, template='public-portfolio-scores-v2-a', num_ctx=16384,
        arithmetic_protocol=old['arithmetic_protocol'], request_protocol=old['request_protocol'],
        tokenizer_sha256=old['tokenizer_sha256'], tokenizer_runtime=old['tokenizer_runtime'])
    if ident['renderer'] != REVERSIBLE_RENDERER:
        raise ValueError('wrong renderer')
    rows = read(legacy / 'development/states.json')
    if len(rows) != 8:
        raise ValueError('eight fixed development inputs required')
    counter = TokenCounter(tokenizer_python, tokenizer_json)
    prompts = []
    for row in rows:
        req, pool = unpack(row)
        rendered = render_for_identity(req, pool, ident)
        n = counter(rendered['prompt'])
        if n + 1024 > 16384:
            raise ValueError('complete development prompt exceeds token allowance')
        prompts.append(dict(state_id=row['state_id'], prompt_tokens=n,
            remaining_tokens=16384-1024-n, prompt_sha256=rendered['prompt_sha256']))
        immutable(root / 'development/inputs' / (row['state_id'] + '.json'), rendered)
    immutable(root / 'development/states.json', rows)
    immutable(root / 'development/offline-token-check.json', dict(prompts=prompts,
        tokenizer_python=str(tokenizer_python), tokenizer_json=str(tokenizer_json), versions=versions,
        python_sha256=digest(tokenizer_python), asset_sha256=digest(tokenizer_json), calls=counter.calls))
    registration = dict(schema='phase2e-a-development-v1', source_sha256=current['sha256'],
        identity=ident, order=[r['state_id'] for r in rows],
        rotation=[rows[i % 8]['state_id'] for i in range(20)],
        stop_rule='stop on first unsuccessful logical request; no skipping or template changes',
        template_selection='fixed R1 template-a; no template race', max_corrections=1)
    immutable(root / 'development/registration.json', registration)
    immutable(root / 'generation-budget.json', dict(protocol='phase2e-durable-generation-v1',
        campaign_id=root.name, limits=LIMITS, phase_max_seconds=TIME_LIMITS,
        max_seconds=7200, max_attempts=120, attempts=[], seconds=0.))
    immutable(root / 'http-budget.json', dict(protocol='phase2e-local-http-v1',
        campaign_id=root.name, max_metadata=400, requests=[]))
    frozen = dict(schema='phase2e-a-freeze-v1', source=current, identity=ident,
        development_registration_sha256=canonical_sha256(registration),
        tokenizer_python=str(tokenizer_python), tokenizer_json=str(tokenizer_json),
        formal_des_limits=dict(online=5120, reference=40960, total=46080),
        generation_limits=LIMITS, phase_max_seconds=TIME_LIMITS,
        parent_sha256=PARENT_SHA, ordinary_test_internal_des=None)
    immutable(root / 'freeze.json', frozen)
    return dict(status='prepared', source_sha256=current['sha256'], states=8,
                max_prompt_tokens=max(r['prompt_tokens'] for r in prompts))


def model_assets(root):
    ident = read(Path(root) / 'freeze.json')['identity']
    if digest(RUNTIME / 'ollama.exe') != ident['runtime_sha256']:
        raise ValueError('fixed Ollama executable mismatch')
    manifest_path = MODELS / 'manifests/registry.ollama.ai/library/qwen3/4b-instruct-2507-q4_K_M'
    if digest(manifest_path) != ident['model_digest'].removeprefix('sha256:'):
        raise ValueError('fixed local model manifest mismatch')
    manifest = read(manifest_path)
    blobs = []
    for row in [manifest['config'], *manifest['layers']]:
        path = MODELS / 'blobs' / row['digest'].replace(':', '-')
        if digest(path) != row['digest'].removeprefix('sha256:') or path.stat().st_size != row['size']:
            raise ValueError('local model blob mismatch')
        blobs.append(dict(path=str(path), sha256=digest(path), bytes=path.stat().st_size))
    immutable(Path(root) / 'hardware/offline-assets.json', dict(runtime=str(RUNTIME),
        runtime_sha256=digest(RUNTIME/'ollama.exe'), model_digest=ident['model_digest'], blobs=blobs))


def telemetry(folder, stop):
    with (folder / 'telemetry.jsonl').open('x', encoding='utf-8') as stream:
        while not stop.is_set():
            try:
                p = subprocess.run(['nvidia-smi', '--query-gpu=timestamp,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw',
                    '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                row = dict(unix=time.time(), gpu_csv=p.stdout.strip(), exit_code=p.returncode,
                    scope='sampled GPU device values, not exact CUDA allocator peaks',
                    allocated_bytes=None, reserved_bytes=None)
            except Exception as e:
                row = dict(unix=time.time(), error=str(e))
            stream.write(json.dumps(row) + '\n')
            stream.flush()
            stop.wait(2)


def development(root, provider):
    root = Path(root)
    if (root / 'development/live-requests.json').exists():
        raise ValueError('live campaign already started; no automatic resubmission')
    rows = read(root / 'development/states.json')
    journal = []
    for index in range(20):
        check_freeze(root)
        row = rows[index % 8]
        request, pool = unpack(row)
        item = dict(index=index, state_id=row['state_id'], status='pending',
                    purpose='coverage' if index < 8 else 'stability')
        journal.append(item)
        save(root / 'development/live-requests.json', journal)
        try:
            record = provider.rank(request, pool, force=True)
            immutable(root / 'development' / f'completion-{index:02d}.json', record)
            immutable(root / 'development' / f'gpu-{index:02d}.json', provider.ranking_receipts[-1])
            item.update(status='completed', cache_key=record['cache_key'],
                attempt_ids=[a['attempt_id'] for a in record['attempts']])
        except Exception as error:
            item.update(status='failed', error=f'{type(error).__name__}: {error}')
            save(root / 'development/live-requests.json', journal)
            raise
        save(root / 'development/live-requests.json', journal)
        print('G1 real completion', index+1, '/20', row['state_id'], flush=True)
    return audit_development(root, persist=True)


def audit_development(root, *, persist=False):
    root = Path(root)
    frozen = read(root / 'freeze.json')
    ident = frozen['identity']
    budget = CampaignBudget(root / 'generation-budget.json').read()
    rows = read(root / 'development/states.json')
    journal = read(root / 'development/live-requests.json') if (root / 'development/live-requests.json').exists() else []
    if len(rows) != 8 or len({r['state_id'] for r in rows}) != 8 or len(journal) > 20:
        raise ValueError('development input/logical-request count differs')
    ids, coverage = set(), set()
    valid = 0
    for index, item in enumerate(journal):
        if item['status'] != 'completed':
            break
        row = rows[index % 8]
        if item['state_id'] != row['state_id'] or item['index'] != index:
            raise ValueError('fixed development order differs')
        req, pool = unpack(row)
        record = read(root / 'development' / f'completion-{index:02d}.json')
        validate_record(record, req, pool, ident)
        attempt_ids = audit_generation_record(root, record, 'development', root/'development/responses',
            read(root / 'development' / f'gpu-{index:02d}.json'), ids)
        if item['cache_key'] != record['cache_key'] or item['attempt_ids'] != attempt_ids:
            raise ValueError('development journal does not bind recorded attempts')
        coverage.add(row['state_id'])
        valid += 1
    pending = sum(a['status'] in ('pending', 'uncertain') for a in budget['attempts'])
    phase_attempts = [a for a in budget['attempts'] if a['phase'] == 'development']
    phase_seconds = sum(a.get('seconds', 0.) for a in phase_attempts)
    deadlines = sum(a.get('seconds',0.) > a['reserved_deadline_seconds'] for a in phase_attempts)
    http = audit_http(root)
    complete = (valid == len(journal) == 20 and len(coverage) == 8 and pending == 0 and
        http['status'] == 'passed' and len(phase_attempts) <= 40 and phase_seconds <= 1800 and
        budget['seconds'] <= 7200 and deadlines == 0)
    if complete and ids != {a['attempt_id'] for a in phase_attempts}:
        raise ValueError('development budget includes unaccounted attempts')
    status = dict(status='passed' if complete else 'partial',
        valid_completions=valid, unique_states=len(coverage), pending_uncertain=pending,
        actual_attempts=len(phase_attempts), generation_seconds=phase_seconds,
        deadline_violations=deadlines, http=http,
        identity_sha256=canonical_sha256(ident), source_sha256=frozen['source']['sha256'],
        new_real_attempt_ids=sorted(ids), remote_requests=0,
        official_jev_accepted=False, ppo_trained=False, value_trained=False)
    if persist:
        save(root / 'development/status.json', status)
    return status


def process_snapshot():
    code = ('Get-CimInstance Win32_Process | Where-Object { $_.Name -in '
            "@('ollama.exe','llama-server.exe') } | Select-Object ProcessId,ParentProcessId,ExecutablePath,CreationDate | ConvertTo-Json -Compress")
    p = subprocess.run(['powershell', '-NoProfile', '-Command', code], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
    v = json.loads(p.stdout) if p.stdout.strip() else []
    return v if isinstance(v, list) else [v]


def live(root, *, port=11459, diagnostic=False):
    root = Path(root)
    frozen = check_freeze(root)
    model_assets(root)
    hardware = root / 'hardware'
    hardware.mkdir(exist_ok=True)
    if (hardware / 'server.json').exists():
        raise ValueError('service receipt exists; no automatic new service or campaign')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
    env = {k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP','TMP','PATH','USERPROFILE','LOCALAPPDATA','APPDATA') if k in os.environ}
    env.update(OLLAMA_MODELS=str(MODELS), OLLAMA_HOST=f'127.0.0.1:{port}', OLLAMA_NO_CLOUD='1',
        OLLAMA_NUM_PARALLEL='1', OLLAMA_KEEP_ALIVE='24h', OLLAMA_DEBUG='1')
    before = process_snapshot()
    stop = Event()
    monitor = Thread(target=telemetry, args=(hardware, stop), daemon=True)
    counter = TokenCounter(frozen['tokenizer_python'], frozen['tokenizer_json'])
    base = f'http://127.0.0.1:{port}'
    p = AuditedProvider(root, root/'development/responses', 'development', base_url=base, counter=counter)
    timer = time.perf_counter()
    result = dict(status='partial')
    proc = None
    try:
        with (hardware/'server.stdout.log').open('xb') as stdout, (hardware/'server.stderr.log').open('xb') as stderr:
            proc = subprocess.Popen([str(RUNTIME/'ollama.exe'), 'serve'], env=env, stdout=stdout, stderr=stderr,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        immutable(hardware/'server.json', dict(pid=proc.pid, base_url=base, executable=str(RUNTIME/'ollama.exe'),
            runtime_sha256=frozen['identity']['runtime_sha256'], owned=True,
            ollama_environment={k:v for k,v in env.items() if k.startswith('OLLAMA_')}, before_processes=before))
        monitor.start()
        # Socket readiness makes no HTTP request; all actual HTTP goes through the ledger.
        ready = False
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError('owned service exited before readiness')
            try:
                with socket.create_connection(('127.0.0.1',port), timeout=.1):
                    ready = True
                    break
            except OSError:
                time.sleep(.1)
        if not ready:
            raise TimeoutError('local service readiness')
        version = p._http('/api/version')
        show = p._http('/api/show', {'model':p.identity['model'], 'verbose':True})
        cold = p._http('/api/ps')
        if (version['version'] != p.identity['runtime_version'] or
                canonical_sha256(show['template']) != p.identity['runtime_template_sha256'] or
                show['details']['quantization_level'] != 'Q4_K_M'):
            raise ValueError('runtime template/quantization identity differs')
        immutable(hardware/'preflight.json', dict(version=version, show=show, initial_ps=cold,
            service_ready_seconds=time.perf_counter()-timer,
            cold_status='model absent from ps' if not cold['models'] else 'model already present'))
        result['G1'] = development(root, p)
        if result['G1']['status'] != 'passed':
            raise ValueError('real development gate incomplete')
        if diagnostic:
            from scripts import phase2e_diagnostic as diag
            result['G2'] = diag.capture(root)
            if result['G2']['status'] != 'inputs_frozen':
                raise ValueError('diagnostic input gate incomplete')
            q = AuditedProvider(root, root/'diagnostics/responses', 'diagnostics', base_url=base, counter=counter)
            result['online'] = diag.online(root, q)
            if result['online']['status'] != 'passed':
                raise ValueError('diagnostic online gate incomplete')
        result['status'] = 'passed'
    except Exception as e:
        result.update(status='partial', error=f'{type(e).__name__}: {e}', traceback=traceback.format_exc())
        if not (root/'development/status.json').exists():
            try:
                partial = audit_development(root)
            except Exception as audit_error:
                partial = dict(status='partial', audit_error=f'{type(audit_error).__name__}: {audit_error}')
            save(root/'development/status.json', dict(partial, error=result['error']))
        print(result['traceback'], file=sys.stderr, flush=True)
    finally:
        stop.set()
        if monitor.ident is not None:
            monitor.join(timeout=15)
        cleanup_errors = []
        try:
            snapshot = process_snapshot()
        except Exception as error:
            snapshot = []
            cleanup_errors.append(f'process snapshot: {type(error).__name__}: {error}')
        children = [x for x in snapshot if proc is not None and x['ParentProcessId'] == proc.pid and x.get('ExecutablePath') and
            Path(x['ExecutablePath']).resolve().is_relative_to(RUNTIME.resolve())]
        stopped = []
        for child in children:
            pid = int(child['ProcessId'])
            # Verify PID, executable, creation time, and parent again immediately before stopping.
            try:
                fresh = next((x for x in process_snapshot() if x['ProcessId'] == pid), None)
                if fresh == child:
                    subprocess.run(['powershell', '-NoProfile', '-Command', f'Stop-Process -Id {pid} -ErrorAction Stop'],
                        capture_output=True, timeout=15, check=True,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    stopped.append(child)
            except Exception as error:
                cleanup_errors.append(f'owned child {pid}: {type(error).__name__}: {error}')
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=20)
            except Exception as error:
                cleanup_errors.append(f'owned server: {type(error).__name__}: {error}')
        try:
            after = process_snapshot()
        except Exception as error:
            after = []
            cleanup_errors.append(f'final process snapshot: {type(error).__name__}: {error}')
        immutable(hardware/'stop-receipt.json', dict(owned_pid=proc.pid if proc else None,
            server_exit_code=proc.returncode if proc else None,
            stopped_owned_children=stopped, before_stop=snapshot, after_stop=after,
            other_processes_stop_requested=False, cleanup_errors=cleanup_errors))
        if cleanup_errors:
            result.update(status='partial', cleanup_errors=cleanup_errors)
        result['end_to_end_seconds'] = time.perf_counter()-timer
        save(root/'tokenizer-live-calls.json', counter.calls)
        save(root/'live-command.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','live','audit-development'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--legacy', type=Path)
    parser.add_argument('--tokenizer-python', type=Path, default=TOKEN_PY)
    parser.add_argument('--tokenizer-json', type=Path, default=TOKEN_JSON)
    parser.add_argument('--diagnostic', action='store_true')
    parser.add_argument('--port', type=int, default=11459)
    args = parser.parse_args(argv)
    if args.action == 'prepare':
        result = prepare(args.root, legacy=args.legacy, tokenizer_python=args.tokenizer_python, tokenizer_json=args.tokenizer_json)
    elif args.action == 'live':
        result = live(args.root, port=args.port, diagnostic=args.diagnostic)
    else:
        result = audit_development(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result['status'] not in ('passed','prepared'))


if __name__ == '__main__':
    raise SystemExit(main())

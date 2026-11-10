"""2E-B persistent local quotas and task-owned Ollama lifecycle. No downloads."""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import math
import os
from pathlib import Path
import socket
import subprocess
from threading import Event, Thread
import time
import uuid

from sim_jev.local_provider import LocalProvider, LocalError
from sim_jev.provenance import canonical_sha256
from scripts.phase2d_r1 import immutable
from scripts.phase2e_live import (read, save, TokenCounter, AuditedProvider, HttpLedger,
    RUNTIME, MODELS, TOKEN_PY, TOKEN_JSON, model_assets, process_snapshot, telemetry)


class Budget:
    def __init__(self, root, slot):
        self.path = Path(root) / 'generation-budget.json'
        self.slot = slot

    def read(self):
        v = read(self.path)
        if (v['max_attempts'], v['max_seconds'], v['max_logical']) != (208, 5400, 104):
            raise LocalError('budget_identity_mismatch')
        if len(v['attempts']) > 208 or len({a['slot'] for a in v['attempts']}) > 104:
            raise LocalError('budget_exceeded')
        if not math.isclose(v['seconds'], math.fsum(a.get('seconds', 0.) for a in v['attempts']), abs_tol=1e-8):
            raise LocalError('budget_time_mismatch')
        return v

    def reserve(self, phase, key, *, logical_request_id=None, attempt_index=None):
        v = self.read()
        if self.slot not in v['slots'] or phase not in ('order', 'new'):
            raise LocalError('unregistered_logical_slot')
        if any(a['status'] != 'completed' for a in v['attempts']):
            raise LocalError('uncertain_prior_generation')
        previous = [a for a in v['attempts'] if a['slot'] == self.slot]
        if (attempt_index not in (0, 1) or len(previous) != attempt_index or
                not logical_request_id or any(a['logical_request_id'] != logical_request_id or
                a['key'] != key for a in previous)):
            raise LocalError('logical_request_attempt_limit')
        left = 5400 - v['seconds']
        if len(v['attempts']) >= 208 or left <= 0:
            raise LocalError('local_budget_exhausted')
        deadline = min(120., left)
        v['attempts'].append(dict(slot=self.slot, phase=phase, key=key, status='pending',
            started_unix=time.time(), attempt_id=uuid.uuid4().hex,
            logical_request_id=logical_request_id, attempt_index=attempt_index,
            reserved_deadline_seconds=deadline))
        save(self.path, v)
        return len(v['attempts']) - 1, deadline

    def finish(self, index, seconds, status, *, artifact=None):
        v = self.read()
        if (status not in ('completed', 'uncertain') or v['attempts'][index]['status'] != 'pending'
                or not math.isfinite(seconds) or seconds < 0):
            raise LocalError('invalid_budget_transition')
        v['attempts'][index].update(seconds=seconds, status=status)
        if artifact is not None:
            v['attempts'][index]['artifact'] = deepcopy(artifact)
        v['seconds'] = math.fsum(a.get('seconds', 0.) for a in v['attempts'])
        save(self.path, v)


class HTTP(HttpLedger):
    def reserve(self, route, data):
        v = read(self.path)
        if v['protocol'] != 'phase2eb-local-http-v1' or v['max_metadata'] != 450:
            raise LocalError('http_budget_identity_mismatch')
        if any(a['status'] != 'completed' for a in v['requests']):
            raise LocalError('uncertain_prior_http')
        if route not in ('/api/generate', '/api/tags', '/api/version', '/api/ps', '/api/show'):
            raise LocalError('unapproved_route')
        kind = 'generation' if route == '/api/generate' else 'metadata'
        if kind == 'metadata' and sum(r['kind'] == kind for r in v['requests']) >= 450:
            raise LocalError('metadata_budget_exhausted')
        aid = None
        if kind == 'generation':
            pending = [a for a in Budget(self.root, '').read()['attempts'] if a['status'] == 'pending']
            if len(pending) != 1:
                raise LocalError('generation_without_reservation')
            aid = pending[0]['attempt_id']
        index = len(v['requests'])
        v['requests'].append(dict(index=index, route=route, kind=kind, status='pending',
            request_sha256=canonical_sha256(data), started_unix=time.time(), generation_attempt_id=aid))
        save(self.path, v)
        return index


class Provider(AuditedProvider):
    def __init__(self, root, slot, base_url, counter):
        self.root = Path(root)
        self.http_ledger = HTTP(root)
        self.token_times = counter
        self.ranking_receipts, self.ranking_persistence_receipts = [], []
        LocalProvider.__init__(self, read(self.root / 'freeze.json')['identity'],
            self.root / 'responses' / slot, mode='live', base_url=base_url,
            budget=Budget(root, slot), phase='order' if slot.startswith('order-') else 'new',
            token_counter=counter)


@contextmanager
def service(root, port=11469):
    """Yield (base URL, counter) or None on unavailable service; always clean own PIDs."""
    root = Path(root)
    hw = root / 'hardware'
    hw.mkdir(exist_ok=True)
    if (hw / 'server.json').exists():
        raise ValueError('owned service already attempted; preserve partial, no automatic retry')
    proc, stop, monitor = None, Event(), None
    before, error = [], None
    counter = TokenCounter(TOKEN_PY, TOKEN_JSON)
    started = time.perf_counter()
    available = None
    try:
        model_assets(root)
        before = process_snapshot()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
        env = {k: os.environ[k] for k in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'PATH',
               'USERPROFILE', 'LOCALAPPDATA', 'APPDATA') if k in os.environ}
        env.update(OLLAMA_MODELS=str(MODELS), OLLAMA_HOST=f'127.0.0.1:{port}',
                   OLLAMA_NO_CLOUD='1', OLLAMA_NUM_PARALLEL='1', OLLAMA_KEEP_ALIVE='24h', OLLAMA_DEBUG='1')
        with (hw / 'server.stdout.log').open('xb') as out, (hw / 'server.stderr.log').open('xb') as err:
            proc = subprocess.Popen([str(RUNTIME / 'ollama.exe'), 'serve'], env=env, stdout=out, stderr=err,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        immutable(hw / 'server.json', dict(pid=proc.pid, executable=str(RUNTIME / 'ollama.exe'),
            owned=True, base_url=f'http://127.0.0.1:{port}', before=before,
            environment={k: v for k, v in env.items() if k.startswith('OLLAMA_')}))
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError('service exited')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise TimeoutError('service readiness')
        p = Provider(root, 'preflight', f'http://127.0.0.1:{port}', counter)
        version = p._http('/api/version')
        show = p._http('/api/show', {'model': p.identity['model'], 'verbose': True})
        ps = p._http('/api/ps')
        if (version['version'] != p.identity['runtime_version'] or
                canonical_sha256(show['template']) != p.identity['runtime_template_sha256'] or
                show['details']['quantization_level'] != 'Q4_K_M'):
            raise ValueError('runtime/template/quantization mismatch')
        immutable(hw / 'preflight.json', dict(version=version, show=show, initial_ps=ps,
            seconds=time.perf_counter() - started, cold_model_absent=not bool(ps['models'])))
        monitor = Thread(target=telemetry, args=(hw, stop), daemon=True)
        monitor.start()
        available = (f'http://127.0.0.1:{port}', counter)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        immutable(hw / 'unavailable.json', dict(status='partial', error=error))
    try:
        yield available
    finally:
        stop.set()
        if monitor is not None:
            monitor.join(timeout=15)
        errors, stopped = [], []
        try:
            snapshot = process_snapshot()
            children = [x for x in snapshot if proc is not None and x['ParentProcessId'] == proc.pid
                        and x.get('ExecutablePath') and Path(x['ExecutablePath']).resolve().is_relative_to(RUNTIME.resolve())]
            for child in children:
                fresh = next((x for x in process_snapshot() if x['ProcessId'] == child['ProcessId']), None)
                if fresh == child:
                    subprocess.run(['powershell', '-NoProfile', '-Command',
                        f"Stop-Process -Id {int(child['ProcessId'])} -ErrorAction Stop"], check=True,
                        capture_output=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    stopped.append(child)
        except Exception as exc:
            errors.append(str(exc))
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except Exception as exc:
                errors.append(str(exc))
        save(root / 'tokenizer-live-calls.json', counter.calls)
        immutable(hw / 'stop-receipt.json', dict(owned_pid=proc.pid if proc else None,
            stopped_owned_children=stopped, cleanup_errors=errors, other_processes_stop_requested=False,
            server_exit_code=proc.returncode if proc else None, availability_error=error))

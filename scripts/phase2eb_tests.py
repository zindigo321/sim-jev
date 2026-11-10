"""Run full/critical regressions with cross-process DES and fixture HTTP accounting."""
import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from sim_jev.provenance import code_fingerprint
from scripts.phase2d_r1 import immutable

INSTRUMENT = r'''
import builtins, functools, os, struct, sys
_counter = os.environ['PHASE2EB_TEST_COUNTER']
_cap = int(os.environ['PHASE2EB_TEST_DES_CAP'])
def _increment(offset):
    with open(_counter,'r+b',buffering=0) as f:
        if os.name == 'nt':
            import msvcrt
            f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX)
        try:
            f.seek(offset); n=struct.unpack('<Q',f.read(8))[0]
            if offset == 0 and n >= _cap: os._exit(92)
            f.seek(offset); f.write(struct.pack('<Q',n+1)); os.fsync(f.fileno())
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f,fcntl.LOCK_UN)
_original_import = builtins.__import__
def _import(*args,**kwargs):
    value = _original_import(*args,**kwargs)
    module=sys.modules.get('sim_jev.planning')
    if module is not None and hasattr(module,'PlanningScenario'):
        original=module.PlanningScenario.evaluate
        if not getattr(original,'_phase2eb_counted',False):
            @functools.wraps(original)
            def counted(*a,**k):
                _increment(0)
                return original(*a,**k)
            counted._phase2eb_counted=True
            module.PlanningScenario.evaluate=counted
        builtins.__import__=_original_import
    return value
builtins.__import__=_import
def _network(event,args):
    if event == 'socket.connect':
        address=args[1]
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1'):
            raise RuntimeError('external test network forbidden')
        _increment(16)
    if event == 'http.client.send':
        b=args[1]
        if isinstance(b,bytes) and b.startswith((b'GET ',b'POST ',b'PUT ',b'HEAD ',b'DELETE ',b'PATCH ',b'OPTIONS ')):
            _increment(8)
sys.addaudithook(_network)
'''

RUNNER = r'''
import json,sys,unittest
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
suite=unittest.defaultTestLoader.discover(str(root/'tests')) if sys.argv[3]=='full' else unittest.defaultTestLoader.loadTestsFromNames([
    'tests.test_phase2eb','tests.test_phase2e_renderer','tests.test_phase2d_r2_renderer',
    'tests.test_phase2e_live','tests.test_phase2e_diagnostic'])
result=unittest.TextTestRunner(verbosity=2).run(suite)
Path(sys.argv[2]).write_text(json.dumps(dict(tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors),
    skipped=len(result.skipped),passed=result.wasSuccessful() and not result.skipped)),encoding='utf8')
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--scope',choices=('full','critical'),default='full')
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    source=code_fingerprint(ROOT)
    helper=args.output/'instrumentation';helper.mkdir()
    (helper/'sitecustomize.py').write_text(INSTRUMENT,encoding='utf8')
    counter=args.output/'actual-counts.bin';counter.write_bytes(bytes(24))
    env=dict(os.environ)
    env['PHASE2EB_TEST_COUNTER']=str(counter.resolve())
    env['PHASE2EB_TEST_DES_CAP']='100000' if args.scope=='full' else '10000'
    env['PYTHONPATH']=str(helper.resolve())+os.pathsep+str(ROOT)+os.pathsep+env.get('PYTHONPATH','')
    start=time.perf_counter()
    with (args.output/'unittest.log').open('w',encoding='utf8') as stream:
        result=subprocess.run([sys.executable,'-c',RUNNER,str(ROOT),str((args.output/'results.json').resolve()),args.scope],
            cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT)
    counts=struct.unpack('<QQQ',counter.read_bytes())
    summary=json.loads((args.output/'results.json').read_text()) if (args.output/'results.json').exists() else {}
    receipt=dict(status='passed' if result.returncode==0 and source==code_fingerprint(ROOT) else 'failed',
        source=source,source_unchanged=source==code_fingerprint(ROOT),scope=args.scope,exit_code=result.returncode,
        actual_des=counts[0],fixture_http_requests=counts[1],loopback_connections=counts[2],
        model_generations=0,external_http=0,seconds=time.perf_counter()-start,**summary,
        accounting='pre-evaluate locked shared counter inherited by Python subprocesses; fixture HTTP separate')
    immutable(args.output/'command.json',receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k!='source'}))
    return result.returncode


if __name__=='__main__':raise SystemExit(main())

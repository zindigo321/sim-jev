"""Bounded runtime regressions; every actual engineering DES is reserved."""
import io
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import patch
from scripts.phase2ec2_common import Journal,write,write_text,sha,ROOT,code_fingerprint
from sim_jev.planning import PlanningScenario


def tests(root):
    root=Path(root); journal=Journal(root); source=code_fingerprint(ROOT)
    if str(ROOT/'tests') not in sys.path: sys.path.insert(0,str(ROOT/'tests'))
    run=len(list((root/'regression').glob('receipt-*.json'))); count=0
    original=PlanningScenario.evaluate
    def counted(self,*args,**kwargs):
        nonlocal count
        key=f'regression/{run}/{count}'; journal.reserve('engineering_des',key); count+=1
        try:
            value=original(self,*args,**kwargs)
            journal.finish(key,dict(score=value['score'],scenario_id=value['scenario_id'])); return value
        except Exception as e:
            journal.finish(key,dict(error=repr(e)),failed=True); raise
    names=['tests.test_phase2ec2','tests.test_phase2ec1','tests.test_phase2eb','tests.test_phase2c_planning_audit','tests.test_phase2c_decision_audit']
    suite=unittest.defaultTestLoader.loadTestsFromNames(names); stream=io.StringIO(); started=time.perf_counter()
    with patch.object(PlanningScenario,'evaluate',counted):
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    path=root/'regression'/f'log-{run}.txt'; write_text(path,stream.getvalue())
    unchanged=source==code_fingerprint(ROOT)
    receipt=dict(passed=result.wasSuccessful() and not result.skipped and unchanged,tests=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),actual_des=count,
        seconds=time.perf_counter()-started,source=source,source_unchanged=unchanged,log_sha256=sha(path),
        scope='critical C1/C2 integration, exact guard, order, freeze, ledger and physical audits; bounded engineering budget')
    write(root/'regression'/f'receipt-{run}.json',receipt)
    if not receipt['passed']: raise ValueError('regression failed: '+str(path))
    return {k:v for k,v in receipt.items() if k!='source'}

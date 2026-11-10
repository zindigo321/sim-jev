"""Critical regressions only, bounded by the requested 1,024 engineering DES."""
import io
import time
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.phase2ec1_common import Journal,write,sha
from sim_jev.planning import PlanningScenario
from sim_jev.provenance import code_fingerprint


def run(root):
    root=Path(root)
    journal=Journal(root)
    test_dir=str(Path(__file__).resolve().parents[1]/'tests')
    if test_dir not in sys.path:sys.path.insert(0,test_dir)
    run_index=len(list((root/'regression').glob('receipt-*.json')))
    names=['tests.test_phase2ec1','tests.test_phase2eb','tests.test_phase2e_renderer',
           'tests.test_phase2c_planning_audit','tests.test_phase2c_decision_audit']
    source=code_fingerprint(Path(__file__).resolve().parents[1])
    original=PlanningScenario.evaluate
    count=0
    def counted(self,*args,**kwargs):
        nonlocal count
        key=f'regression/{run_index}/{count}'
        journal.reserve('engineering_des',key)
        count+=1
        try:
            result=original(self,*args,**kwargs)
            journal.finish(key,dict(score=result['score'],scenario_id=result['scenario_id']))
            return result
        except Exception as e:
            journal.finish(key,dict(error=repr(e)),failed=True)
            raise
    suite=unittest.defaultTestLoader.loadTestsFromNames(names)
    stream=io.StringIO()
    start=time.perf_counter()
    with patch.object(PlanningScenario,'evaluate',counted):
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    log=root/'regression'/f'log-{run_index}.txt'
    log.parent.mkdir(parents=True,exist_ok=True)
    log.write_text(stream.getvalue(),encoding='utf8')
    receipt=dict(passed=result.wasSuccessful() and not result.skipped,tests=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),
        actual_des=count,seconds=time.perf_counter()-start,source=source,
        source_unchanged=source==code_fingerprint(Path(__file__).resolve().parents[1]),log_sha256=sha(log),
        scope='critical behavior; full historical suite exceeds this campaign engineering DES cap')
    write(root/'regression'/f'receipt-{run_index}.json',receipt)
    if not receipt['passed'] or not receipt['source_unchanged']:
        raise ValueError('critical regression failed; see '+str(log))
    return {k:v for k,v in receipt.items() if k!='source'}

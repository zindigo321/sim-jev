"""Run the unchanged full test suite with an explicit network policy and receipt."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from sim_jev.provenance import code_fingerprint
from scripts.phase2d_r2_delivery import write

# These original tests intentionally create HTTP/TLS fixture servers. They are
# retained unchanged and explicitly unverified under the user's zero-HTTP rule.
NETWORK_TESTS={
 'test_phase2c_jev.JevTests.test_real_local_http_slow_drip_respects_total_deadline',
 'test_phase2c_jev_acceptance.JevAcceptanceTests.test_default_transport_refuses_same_origin_cross_port_and_cross_host_redirects',
 'test_phase2c_jev_acceptance.JevAcceptanceTests.test_https_to_http_downgrade_never_reaches_local_target',
 'test_phase2c_jev_review.IndependentJevReviewTests.test_real_local_tls_redirect_and_error_never_leak_credentials_to_http_target_or_audit',
}
def flatten(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):yield from flatten(item)
        else:yield item
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--allow-fixture-http',action='store_true',help='requires explicit user authorization; only temporary localhost fixture servers')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    start=time.perf_counter();source=code_fingerprint(ROOT)
    counts={'fixture_http_requests':0,'socket_connections':0}
    def guard(event,values):
        if event=='socket.connect':
            address=values[1]
            if not args.allow_fixture_http:raise AssertionError('network forbidden by zero-HTTP test policy')
            if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1'):
                raise AssertionError('external test socket forbidden')
            counts['socket_connections']+=1
        if event=='http.client.send':
            payload=values[1]
            if isinstance(payload,bytes) and payload.startswith((b'GET ',b'POST ',b'HEAD ',b'PUT ',b'DELETE ',b'PATCH ',b'OPTIONS ')):
                if not args.allow_fixture_http:raise AssertionError('HTTP forbidden by zero-HTTP test policy')
                counts['fixture_http_requests']+=1
    sys.addaudithook(guard)
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    selected=[];policy_excluded=[]
    for test in flatten(suite):
        # Match the original method as well as class: its class is intentionally
        # read from discovery rather than assumed by this wrapper.
        restricted=any(test.id().split('.')[-1]==name.split('.')[-1] for name in NETWORK_TESTS)
        if restricted and not args.allow_fixture_http:
            policy_excluded.append(test.id())
            def skip(test=test):raise unittest.SkipTest('explicit zero-HTTP constraint; original local HTTP/TLS test retained unchanged')
            setattr(test,test._testMethodName,skip)
        selected.append(test)
    with (args.output/'unittest.log').open('x',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.TestSuite(selected))
    status='passed' if result.wasSuccessful() and not policy_excluded and not result.skipped else 'partial' if result.wasSuccessful() else 'failed'
    receipt=dict(status=status,argv=sys.orig_argv,cwd=str(Path.cwd()),interpreter=sys.executable,
                 python=sys.version,source=source,source_unchanged=source==code_fingerprint(ROOT),
                 discovered=len(selected),tests_run=result.testsRun,passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
                 failed=len(result.failures),errors=len(result.errors),skipped=[{'test':test.id(),'reason':reason} for test,reason in result.skipped],
                 zero_http_policy_excluded=policy_excluded,seconds=time.perf_counter()-start,**counts,
                 ordinary_test_internal_des=None,total_engineering_des=None,
                 model_generations=0,metadata_requests=0,external_http_requests=0,
                 exit_code=0 if result.wasSuccessful() and source==code_fingerprint(ROOT) else 1)
    write(args.output/'command.json',receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k!='source'},ensure_ascii=False))
    return 0 if result.wasSuccessful() and receipt['source_unchanged'] else 1
if __name__=='__main__':raise SystemExit(main())

"""Frozen C2 campaign: prepare/probe/freeze/online/diagnose/seal/reference/analyze/audit/package."""
from pathlib import Path
import argparse
import json
import sys
import time
import traceback
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','probe','freeze','online','diagnose','seal','reference','analyze','audit','package','tests'])
    p.add_argument('--root',type=Path,required=True)
    a=p.parse_args()
    from scripts.phase2ec2_common import append,write
    started=time.perf_counter(); run_id=str(time.time_ns())
    append(a.root/'commands.jsonl',dict(id=run_id,event='start',unix=time.time(),argv=sys.argv,
        interpreter=sys.executable,cwd=str(Path.cwd())))
    modules=dict(prepare='prepare',freeze='prepare',probe='probe',online='experiment',diagnose='experiment',
        seal='experiment',reference='experiment',analyze='analysis',audit='audit_readonly',package='delivery',tests='tests')
    try:
        import importlib
        m=importlib.import_module('scripts.phase2ec2_'+modules[a.action])
        result=getattr(m,a.action)(a.root)
        append(a.root/'commands.jsonl',dict(id=run_id,event='completed',unix=time.time(),seconds=time.perf_counter()-started))
        print(json.dumps(result,ensure_ascii=False),flush=True)
    except Exception as e:
        error=dict(id=run_id,event='failed',action=a.action,error=repr(e),traceback=traceback.format_exc(),
            unix=time.time(),seconds=time.perf_counter()-started)
        write(a.root/'failures'/f'{a.action}-{run_id}.json',error)
        append(a.root/'commands.jsonl',error)
        raise


if __name__=='__main__': main()

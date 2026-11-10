"""2E-C1 CLI: staged real training and blinded new-scarce evaluation."""
from pathlib import Path
import argparse
import json
import sys
import traceback
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','probe','resume-smoke','cache','train','seal','online',
                                   'reference','analyze','audit','package','tests','compatibility','runtime-probe','report'])
    p.add_argument('--root',type=Path,required=True)
    args = p.parse_args()
    try:
        if args.action == 'prepare':
            from scripts.phase2ec1_data import prepare
            result = prepare(args.root)
        elif args.action in ('probe','resume-smoke','cache','train'):
            from scripts import phase2ec1_training as t
            result = getattr(t,args.action.replace('-','_'))(args.root)
        elif args.action in ('seal','online','reference','compatibility','runtime-probe'):
            from scripts import phase2ec1_experiment as e
            result = getattr(e,args.action.replace('-','_'))(args.root)
        elif args.action in ('analyze','audit','package','report'):
            from scripts import phase2ec1_analysis as a
            result = getattr(a,args.action)(args.root)
        else:
            from scripts.phase2ec1_tests import run
            result = run(args.root)
        print(json.dumps(result,ensure_ascii=False),flush=True)
    except Exception as e:
        from scripts.phase2ec1_common import write
        write(args.root/'failures'/f'{args.action}-{time.time_ns()}.json',
              dict(action=args.action,error=repr(e),traceback=traceback.format_exc(),unix=time.time()))
        raise


if __name__ == '__main__':
    main()

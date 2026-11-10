"""Complete old public-state inventory, without advancing any trajectory."""
from pathlib import Path
import os
import time
from scripts.phase2ec1_common import ROOT,read,write,sha,canonical_sha256,append,lines
from scripts.phase2e_diagnostic import request_pool_identity,request_from_historical_facts
from scripts.phase2d_local import unpack
from sim_jev.candidates import generate_candidates
from sim_jev.weekly import WeeklySimulation
from sim_jev.decision import DecisionRequest


def complete(root):
    root=Path(root)
    if (root/'states').exists():raise ValueError('history completion must precede holdout states')
    known=read(root/'history/seen-public.json')+read(root/'history/supplement-public.json')
    exact={v['exact_input_sha256'] for v in known}
    requests={canonical_sha256(v['request']) for v in known}
    rows,resolved=[],[]
    for gap in read(root/'history/supplement-inventory.json')['unavailable_historical_reconstructions']:
        value=read(gap['path'])
        if not value.get('audit_only'):
            raise ValueError('unresolved historical checkpoint: '+gap['path'])
        for record in value['weekly_state']['decision_records']:
            req=DecisionRequest.from_dict(record['request'])
            key=canonical_sha256(req.to_dict())
            if key in requests:continue
            requests.add(key)
            pool=sorted(generate_candidates(req,16,20261102),key=lambda c:c.id)
            a,b=request_pool_identity(req.to_dict(),[c.to_dict() for c in pool])
            if a not in exact:
                exact.add(a)
                rows.append(dict(origin=gap['path']+'#public-request-regenerated-pool',request=req.to_dict(),
                    pool=[c.to_dict() for c in pool],exact_input_sha256=a,business_input_sha256=b))
        resolved.append(dict(path=gap['path'],sha256=sha(gap['path']),
            resolution='audit-only branch wrapper; legal request is available; pool regenerated without DES'))
    write(root/'history/supplement-completion.json',rows)
    write(root/'history/completion-receipt.json',dict(resolved=resolved,unresolved=0,added_states=len(rows),
        new_des=0,new_trajectories=0,unix=time.time()))
    return dict(resolved=len(resolved),added_states=len(rows),unresolved=0)


def collect(root):
    root=Path(root)
    if (root/'states').exists():raise ValueError('history must be complete before new states')
    known={s['exact_input_sha256'] for s in read(root/'history/seen-public.json')}
    rows=lines(root/'history/supplement-progress.jsonl')
    known.update(r['exact_input_sha256'] for r in rows)
    sources,saved_files,errors=[],lines(root/'history/supplement-files.jsonl'),[]
    seen_files={v['sha256'] for v in saved_files}
    sources.extend(saved_files)
    def add(item,origin):
        req,pool=unpack(item)
        exact,business=request_pool_identity(req.to_dict(),[c.to_dict() for c in pool])
        if exact in known:return
        known.add(exact)
        row=dict(origin=origin,request=req.to_dict(),pool=[c.to_dict() for c in pool],
                 exact_input_sha256=exact,business_input_sha256=business)
        rows.append(row)
        append(root/'history/supplement-progress.jsonl',row)
    for current,dirs,names in os.walk(ROOT/'runs'):
        dirs[:]=[d for d in dirs if d not in ('branch-audit-store','evaluation','responses','source','__pycache__','.git')]
        for name in names:
            if name not in ('states.json','state.json','seen-public-states.json','checkpoint.json','weekly-state.json'):
                continue
            path=Path(current)/name
            digest=sha(path)
            if digest in seen_files:continue
            seen_files.add(digest)
            value=read(path)
            source=dict(path=str(path),sha256=digest)
            sources.append(source)
            items=value if isinstance(value,list) else [value]
            for item in items:
                if isinstance(item,dict) and 'request' in item and 'pool' in item:
                    add(item,str(path))
            if not isinstance(value,dict):continue
            weekly=value.get('weekly_state',value if name=='weekly-state.json' else {})
            for record in weekly.get('decision_records',[]):
                if 'request' in record and 'pool' in record:
                    add(record,str(path)+'#decision')
            # Prior mandatory-first snapshots contain complete historical facts.
            records=weekly.get('decision_records',[])
            if name=='checkpoint.json' and weekly and (not records or any('pool' not in r for r in records)):
                try:
                    sim=WeeklySimulation.from_checkpoint(value)
                    for point in weekly['processed_week_points']:
                        req=request_from_historical_facts(sim,point)
                        pool=sorted(generate_candidates(req,16,20261102),key=lambda c:c.id)
                        add(dict(request=req.to_dict(),pool=[c.to_dict() for c in pool]),str(path)+'#'+str(point))
                except (ValueError,KeyError,TypeError) as exc:
                    errors.append(dict(path=str(path),error=repr(exc)))
            append(root/'history/supplement-files.jsonl',source)
            if len(sources)%100==0:print('history files',len(sources),'new public states',len(rows),flush=True)
    write(root/'history/supplement-public.json',rows)
    write(root/'history/supplement-inventory.json',dict(sources=sources,rows=len(rows),
        unavailable_historical_reconstructions=errors,unix=time.time(),new_des=0,new_trajectories=0))
    return dict(public_states_added=len(rows),source_files=len(sources),reconstruction_gaps=len(errors))


if __name__=='__main__':
    import sys,json
    print(json.dumps((complete if '--complete' in sys.argv else collect)(Path(sys.argv[1]))))

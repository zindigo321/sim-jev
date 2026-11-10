"""Model sealing, genuine timed online decisions, and the test-label barrier."""
from pathlib import Path
import shutil
import time
import traceback
import sys
import numpy as np
from scripts.phase2ec1_common import (ROOT,read,write,sha,manifest,verify_files,
    Journal,canonical_sha256,lines)
from scripts.phase2eb import capture,evaluate
from scripts.phase2d_local import unpack
from sim_jev.provenance import code_fingerprint
from sim_jev.decision_resolver import resolve_decision
from sim_jev.qwen_pointer import CandidateHeadPolicySpec
from sim_jev.head_decision import HeadDecisionRuntime


def compatibility(root):
    """Two branches in each existing interpreter; same public input and CRN."""
    import importlib.metadata
    root=Path(root)
    tag=Path(sys.executable).parent.name
    d=read(root/'data/dataset.json')[0]
    req,pool=unpack(d['state'])
    journal=Journal(root)
    rows,means,timing=evaluate(root,journal,d['state'],req,pool,[pool[0].id],2,
        read(root/'parent/freeze.json')['online_seed'],'engineering_des','compatibility/'+tag)
    scientific=[{k:v for k,v in r.items() if k not in ('compute_seconds','audit_seconds')} for r in rows]
    value=dict(interpreter=sys.executable,python=sys.version,executable_sha256=sha(sys.executable),
               packages=sorted([dict(name=x.metadata['Name'],version=x.version) for x in importlib.metadata.distributions()],
                               key=lambda x:x['name'].lower()),
               scientific_sha256=canonical_sha256(scientific),actual_des=2,means=means)
    write(root/'compatibility'/(tag+'.json'),value)
    return dict(interpreter=tag,scientific_sha256=value['scientific_sha256'],actual_des=2)


def runtime_probe(root):
    """Real normal-entry integration, on one previously seen train state only."""
    from scripts.phase2ec1_assets import DEST
    from sim_jev.qwen_pointer_runtime import FrozenQwen,load_head
    root=Path(root)
    journal=Journal(root)
    backbone=FrozenQwen(DEST,journal,root/'runtime-probe/load.json')
    heads={k:load_head(read(root/'models'/f'{k}11-selection.json')['selected_path'])[0] for k in ('P','S')}
    state=next(d['state'] for d in read(root/'data/dataset.json') if d['state']['scenario']=='scarce' and d['state']['split']=='train')
    request,pool=unpack(state)
    records=[]
    for method in ('H32','H42','A8','S8','P8'):
        namespace='runtime-probe/'+method
        runtime=HeadDecisionRuntime(backbone=backbone,p_head=heads['P'],s_head=heads['S'],a_model=read(root/'models/A.json'),
            evaluate=lambda req,pool,ids,b,seed:evaluate(root,journal,state,req,pool,ids,b,seed,'engineering_des',namespace),
            evidence_writer=lambda r:write(root/namespace/'selection-evidence.json',r),slot=namespace,expected_pool=state['pool'])
        result=resolve_decision(request,CandidateHeadPolicySpec(method=method,planning_seed=read(root/'registration.json')['online_seed']),runtime)
        write(root/namespace/'action.json',result.record)
        records.append(result.record)
    write(root/'runtime-probe/result.json',dict(status='passed',state_id=state['state_id'],
        des=sum(r['actual_des'] for r in records),forwards=sum(r['forward_calls'] for r in records),frozen=backbone.prove_frozen()))
    return read(root/'runtime-probe/result.json')


def seal(root):
    root = Path(root)
    if (root/'states').exists():
        raise ValueError('new states already exist before model seal')
    if read(root/'probe/gate.json')['status']!='passed':
        raise ValueError('engineering probe did not pass')
    if read(root/'runtime-probe/result.json')['status']!='passed':
        raise ValueError('normal decision entry probe did not pass')
    if read(root/'compatibility/sim-jev.json')['scientific_sha256'] != read(root/'compatibility/cgfedllm.json')['scientific_sha256']:
        raise ValueError('interpreter DES compatibility failed')
    regressions=sorted((root/'regression').glob('receipt-*.json'))
    if not regressions or not read(regressions[-1])['passed']:
        raise ValueError('critical regression failed')
    if read(root/'history/completion-receipt.json')['unresolved']:
        raise ValueError('historical public-state reconstruction gaps remain')
    for kind in ('P','S'):
        selection = read(root/'models'/f'{kind}11-selection.json')
        path = Path(selection['selected_path'])
        if sha(path)!=selection['selected_sha256']:
            raise ValueError('selected checkpoint changed')
        shutil.copyfile(path,root/'models'/f'{kind}11-selected.pt')
    source = code_fingerprint(ROOT)
    for row in source['files']:
        p = root/'source'/row['path']
        p.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/row['path'],p)
    shutil.copytree(ROOT/'docs',root/'source/docs',dirs_exist_ok=True)
    files = []
    for name in ('models','checkpoints','training','probe','cache','data','source','history','assets','runtime-probe','compatibility'):
        files.extend(dict(r,path=name+'/'+r['path']) for r in manifest(root/name))
    files += [dict(path=name,sha256=sha(root/name)) for name in
              ('registration.json','kernel-freeze.json','engineering-revisions.json','requested-prompt.txt')]
    result = dict(source=source,files=files,files_sha256=canonical_sha256(files),
                  sealed_unix=time.time(),new_states_built=0)
    write(root/'model-seal.json',result)
    return dict(status='sealed',source_sha256=source['sha256'])


def check(root):
    root = Path(root)
    seal = read(root/'model-seal.json')
    if canonical_sha256(seal['files'])!=seal['files_sha256']:
        raise ValueError('model seal digest mismatch')
    verify_files(root,seal['files'])
    if code_fingerprint(ROOT)!=seal['source']:
        raise ValueError('implementation changed after model seal')
    return read(root/'registration.json')


def online(root):
    import torch
    from scripts.phase2ec1_assets import DEST
    from sim_jev.qwen_pointer_runtime import FrozenQwen,HardwareSampler,load_head
    root = Path(root)
    frozen = check(root)
    write(root/'online-started.json',dict(unix=time.time(),model_seal_sha256=sha(root/'model-seal.json')))
    journal = Journal(root)
    sampler = HardwareSampler(root/'hardware/online.jsonl')
    model = FrozenQwen(DEST,journal,root/'online-load.json')
    p_head,p_ck = load_head(root/'models/P11-selected.pt')
    s_head,s_ck = load_head(root/'models/S11-selected.pt')
    a_model = read(root/'models/A.json')
    warm_state=read(root/'data/dataset.json')[0]['state']
    _,warm_pool=unpack(warm_state)
    warm_c,warm_q,warm_receipt=model.forward(warm_pool,'online-warmup-known-train')
    with torch.no_grad():
        p_head(warm_c,warm_q)
    prior = (read(root/'history/seen-public.json')+read(root/'history/supplement-public.json')+
             read(root/'history/supplement-completion.json'))
    actions,permutations,failures = [],[],[]
    prng = np.random.default_rng(frozen['test_permutation_seed'])
    for plan in frozen['planned']:
        sid = plan['state_id']
        key = 'trajectory/'+sid
        journal.reserve('trajectory',key)
        t = time.perf_counter()
        try:
            state = capture(root,plan,prior)
            journal.finish(key,dict(state_sha256=sha(root/'states'/sid/'state.json'),simulated_hours=336),
                           'states/'+sid+'/state.json',seconds=time.perf_counter()-t)
        except Exception as e:
            failure = dict(state_id=sid,scope='construction',error=repr(e),traceback=traceback.format_exc())
            journal.finish(key,failure,failed=True,seconds=time.perf_counter()-t)
            write(root/'failures'/(sid+'-construction.json'),failure)
            failures.append(failure)
            continue
        req,pool = unpack(state)
        methods = frozen['methods']
        offset = plan['index']%len(methods)
        methods = methods[offset:]+methods[:offset]
        write(root/'test'/sid/'method-order.json',methods)
        def decide(method,tag,perm=None):
            namespace = ('permutation' if perm is not None else 'test')+'/'+sid+'/'+tag
            def evaluator(request,pool,retained,b,seed):
                return evaluate(root,journal,state,request,pool,retained,b,seed,
                    'permutation' if perm is not None else 'online',namespace)
            runtime = HeadDecisionRuntime(backbone=model,p_head=p_head,s_head=s_head,a_model=a_model,
                evaluate=evaluator,evidence_writer=lambda r:write(root/namespace/'selection-evidence.json',r),
                slot=namespace,expected_pool=state['pool'],permutation=perm)
            started = time.perf_counter()
            try:
                result = resolve_decision(req,CandidateHeadPolicySpec(method=method,planning_seed=frozen['online_seed']),runtime)
                record = dict(result.record,state_id=sid,status='completed',scenario='scarce',tag=tag)
            except Exception as e:
                record = dict(state_id=sid,method=method,tag=tag,status='failed',error=repr(e),
                              traceback=traceback.format_exc(),failed_path_seconds=time.perf_counter()-started)
                failures.append(record)
            write(root/namespace/'action.json',record)
            return record
        for method in methods:
            actions.append(decide(method,method))
        if plan['index'] in frozen['permutation_slots'] and len(pool)>=2:
            registered = [list(reversed(range(len(pool)))),prng.permutation(len(pool)).tolist()]
            write(root/'permutation'/sid/'registered-orders.json',registered)
            for pi,perm in enumerate(registered):
                permutations.append(dict(decide('P8','order'+str(pi),perm),permutation=perm))
        print('online',plan['index']+1,sid,'eligible',state['primary_eligible'] and len(pool)>=4,
              'forward',journal.counts['forward'],'DES',journal.counts['online'],flush=True)
    write(root/'test/actions.json',actions)
    write(root/'permutation/actions.json',permutations)
    write(root/'online-failures.json',failures)
    write(root/'online-backbone-frozen.json',model.prove_frozen())
    sampler.close()
    files = []
    for name in ('test','permutation','states','forwards','hardware','models','checkpoints','training'):
        files.extend(dict(r,path=name+'/'+r['path']) for r in manifest(root/name))
    for name in ('compute-journal.jsonl','model-seal.json','registration.json','online-failures.json','online-backbone-frozen.json'):
        # Ledger is sealed by snapshot; reference will append its own entries later.
        if name=='compute-journal.jsonl':
            shutil.copyfile(root/name,root/'online-compute-journal.jsonl')
            name='online-compute-journal.jsonl'
        files.append(dict(path=name,sha256=sha(root/name)))
    verify_files(root,files)
    value = dict(files=files,files_sha256=canonical_sha256(files),sealed_unix=time.time(),
                 counts=dict(journal.counts),model_seal_sha256=sha(root/'model-seal.json'))
    write(root/'online-action-seal.json',value)
    return dict(status='actions_sealed',methods=len(actions),permutations=len(permutations),failures=len(failures),counts=dict(journal.counts))


def reference(root):
    root = Path(root)
    frozen = check(root)
    seal = read(root/'online-action-seal.json')
    if canonical_sha256(seal['files'])!=seal['files_sha256']:
        raise ValueError('action seal digest mismatch')
    verify_files(root,seal['files'])
    write(root/'reference-started.json',dict(unix=time.time(),action_seal_sha256=sha(root/'online-action-seal.json')))
    journal = Journal(root)
    results = []
    for plan in frozen['planned']:
        sid = plan['state_id']
        path = root/'states'/sid/'state.json'
        if not path.exists():
            continue
        s = read(path)
        req,pool = unpack(s)
        try:
            rows,means,timing = evaluate(root,journal,s,req,pool,[c.id for c in pool],32,
                                        frozen['reference_seed'],'reference','reference/'+sid)
            results.append(dict(state_id=sid,status='completed',q_ref=means,actual_des=len(rows),timing=timing))
        except Exception as e:
            failure = dict(state_id=sid,status='failed',error=repr(e),traceback=traceback.format_exc())
            write(root/'failures'/(sid+'-reference.json'),failure)
            results.append(failure)
        print('reference',plan['index']+1,sid,journal.counts['reference'],flush=True)
    write(root/'reference-summary.json',results)
    return dict(states=len(results),counts=dict(journal.counts))

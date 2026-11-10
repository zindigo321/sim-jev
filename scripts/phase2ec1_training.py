"""Real frozen-backbone probes, sharded feature extraction and CPU head training."""
from pathlib import Path
import gc
import json
import math
import os
import subprocess
import sys
import time
import numpy as np
import torch
from scripts.phase2ec1_common import read,write,sha,Journal,canonical_sha256,append
from scripts.phase2ec1_assets import DEST
from scripts.phase2ec1_data import soft_targets
from scripts.phase2d_local import unpack
from sim_jev.qwen_pointer import encode,structured
from sim_jev.qwen_pointer_runtime import (SharedPointerHead,FrozenQwen,HardwareSampler,
                                         weight_digest,load_head)
from sim_jev.residual_ranker import ranked_ids


def save_checkpoint(path,head,opt,*,epoch,updates,rng,extra=None):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    start = time.perf_counter()
    with path.open('xb') as f:
        torch.save(dict(model=head.state_dict(),optimizer=opt.state_dict(),
            dims=[head.candidate_dim,head.query_dim],epoch=epoch,updates=updates,
            torch_rng=torch.get_rng_state(),numpy_rng=json.dumps(rng.bit_generator.state),
            extra=extra or {}),f)
        f.flush()
        os.fsync(f.fileno())
    return dict(path=str(path),sha256=sha(path),tensor_sha256=weight_digest(head),
                seconds=time.perf_counter()-start,bytes=path.stat().st_size)


def loss(head,samples):
    # Average within an environment before averaging the environment groups.
    return torch.stack([-(target*torch.log_softmax(head(c,q),dim=-1)).sum()
                        for c,q,target in samples]).mean()


def update(head,opt,samples,journal,phase,key):
    journal.reserve(phase,key)
    start = time.perf_counter()
    try:
        opt.zero_grad(set_to_none=True)
        value = loss(head,samples)
        if not torch.isfinite(value):
            raise ValueError('nonfinite training loss')
        value.backward()
        norm = torch.nn.utils.clip_grad_norm_(head.parameters(),1.)
        if not torch.isfinite(norm):
            raise ValueError('nonfinite gradient norm')
        opt.step()
        row = dict(loss=value.item(),gradient_norm=norm.item(),seconds=time.perf_counter()-start)
        journal.finish(key,row,seconds=row['seconds'])
        return row
    except Exception as e:
        journal.finish(key,dict(error=repr(e)),failed=True,seconds=time.perf_counter()-start)
        raise


def probe(root):
    root = Path(root)
    journal = Journal(root)
    data = read(root/'data/dataset.json')
    train = [d for d in data if d['state']['split']=='train']
    chosen = [d for scene in ('balanced','scarce')
              for d in [v for v in train if v['state']['scenario']==scene][:2]]
    sampler = HardwareSampler(root/'hardware/probe.jsonl')
    model = FrozenQwen(DEST,journal,root/'probe/bf16-load.json')
    lengths = []
    for d in data:
        _,pool = unpack(d['state'])
        lengths.append(dict(state_id=d['state']['state_id'],tokens=len(encode(pool,model.tokenizer)['input_ids']),M=len(pool)))
    write(root/'probe/input-lengths.json',lengths)
    samples, ids, receipts = [], [], []
    for i,d in enumerate(chosen):
        _,pool = unpack(d['state'])
        if i==0:
            pool = pool[:2]
            target,_ = soft_targets(np.asarray(d['labels']['reference'])[:2],np.random.default_rng(11))
        else:
            target = np.asarray(d['labels']['target'])
        c,q,e = model.forward(pool,f'probe-bf16-{i}')
        samples.append((c.clone(),q.clone(),torch.tensor(target,dtype=torch.float32)))
        ids.append([v.id for v in pool])
        receipts.append(e)
    # One fresh call checks causal prefix behavior: first candidate stays fixed.
    _,pool = unpack(chosen[1]['state'])
    reordered = [pool[0],*pool[:0:-1]]
    c2,q2,e2 = model.forward(reordered,'probe-causal-prefix')
    causal = dict(first_candidate_exact=torch.equal(samples[1][0][0],c2[0]),
                  decision_changed=not torch.equal(samples[1][1],q2))
    if not causal['first_candidate_exact'] or not causal['decision_changed']:
        raise ValueError('native causal-prefix behavior failed')
    torch.manual_seed(11)
    head = SharedPointerHead()
    opt = torch.optim.AdamW(head.parameters(),lr=.001,weight_decay=.01)
    rng = np.random.default_rng(11)
    initial = weight_digest(head)
    initial_loss = loss(head,samples).item()
    for step in range(39):
        row = update(head,opt,samples,journal,'smoke_update',f'smoke/{step+1}')
        append(root/'probe/smoke-loss.jsonl',dict(step=step+1,**row))
    with (root/'probe/samples.pt').open('xb') as f:
        torch.save(dict(samples=samples,ids=ids),f)
    checkpoint = save_checkpoint(root/'probe/smoke-step39.pt',head,opt,epoch=0,updates=39,rng=rng,
                                extra=dict(initial_loss=initial_loss,initial_digest=initial))
    with torch.no_grad():
        predictions = [head(c,q).tolist() for c,q,_ in samples]
    loaded,_ = load_head(root/'probe/smoke-step39.pt')
    if predictions != [loaded(c,q).tolist() for c,q,_ in samples]:
        raise ValueError('smoke head reload differs')
    frozen = model.prove_frozen()
    write(root/'probe/bf16-result.json',dict(frozen=frozen,causal=causal,checkpoint=checkpoint,
        initial_loss=initial_loss,step39_loss=loss(head,samples).item(),predictions=predictions,
        before_head_sha256=initial,after_head_sha256=weight_digest(head),state_ids=[d['state']['state_id'] for d in chosen],
        pool_sizes=[len(x) for x in ids],max_tokens=max(r['tokens'] for r in lengths)))
    # A separate official checkpoint load, so FP32 is not an upcast BF16 model.
    del model
    gc.collect()
    torch.cuda.empty_cache()
    try:
        fp = FrozenQwen(DEST,journal,root/'probe/fp32-load.json',dtype='float32')
        _,pool = unpack(chosen[1]['state'])
        c,q,e = fp.forward(pool,'probe-fp32')
        x,y = samples[1][0],samples[1][1]
        comparison = dict(status='passed',candidate_max_abs=(x-c).abs().max().item(),
            candidate_relative_l2=((x-c).norm()/c.norm()).item(),query_max_abs=(y-q).abs().max().item(),
            memory=e['memory_after'],frozen=fp.prove_frozen())
        del fp
        gc.collect()
        torch.cuda.empty_cache()
    except torch.cuda.OutOfMemoryError as exc:
        comparison = dict(status='out_of_memory',error=repr(exc),selected_dtype='bfloat16')
    write(root/'probe/dtype-comparison.json',comparison)
    sampler.close()
    # Continuation is a fresh process; the 40th update is executed exactly once.
    command = [sys.executable,str(Path(__file__).with_name('phase2ec1.py')),'resume-smoke','--root',str(root)]
    p = subprocess.run(command,capture_output=True,text=True,encoding='utf8',errors='replace',
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    write(root/'probe/resume-process.json',dict(argv=command,exit_code=p.returncode,stdout=p.stdout,stderr=p.stderr))
    if p.returncode:
        raise ValueError('fresh-process smoke continuation failed')
    write(root/'probe/gate.json',dict(status='passed',dtype='bfloat16',kernel='native-causal-sdpa',
        one_forward=True,head_updated=initial!=weight_digest(head),formal_training_allowed=True,
        completed_unix=time.time(),small_pool_smoke='first two candidates from one real train pool; labels recomputed from its DES branches'))
    return read(root/'probe/gate.json')


def resume_smoke(root):
    root = Path(root)
    torch.set_num_threads(1)
    head,ck = load_head(root/'probe/smoke-step39.pt')
    opt = torch.optim.AdamW(head.parameters(),lr=.001,weight_decay=.01)
    opt.load_state_dict(ck['optimizer'])
    torch.set_rng_state(ck['torch_rng'])
    rng = np.random.default_rng()
    rng.bit_generator.state = json.loads(ck['numpy_rng'])
    samples = torch.load(root/'probe/samples.pt',map_location='cpu',weights_only=True)['samples']
    expected = read(root/'probe/bf16-result.json')['predictions']
    actual = [head(c,q).tolist() for c,q,_ in samples]
    if expected != actual:
        raise ValueError('fresh process load logits differ')
    before = weight_digest(head)
    journal = Journal(root)
    row = update(head,opt,samples,journal,'smoke_update','smoke/40')
    after_loss = loss(head,samples).item()
    if not after_loss < ck['extra']['initial_loss'] or weight_digest(head)==before:
        raise ValueError('real-label fitting/continuation failed')
    receipt = save_checkpoint(root/'probe/smoke-step40.pt',head,opt,epoch=0,updates=40,rng=rng)
    result = dict(status='passed',pid=os.getpid(),parent_checkpoint_sha256=sha(root/'probe/smoke-step39.pt'),
        reload_logits_exact=True,optimizer_steps_before=39,optimizer_steps_after=40,
        initial_loss=ck['extra']['initial_loss'],final_loss=after_loss,update=row,checkpoint=receipt)
    write(root/'probe/resume-receipt.json',result)
    return result


def cache(root):
    root = Path(root)
    if read(root/'probe/gate.json')['status']!='passed':
        raise ValueError('real engineering probe must pass first')
    journal = Journal(root)
    model = FrozenQwen(DEST,journal,root/'cache/load.json')
    sampler = HardwareSampler(root/'hardware/cache.jsonl')
    rows = []
    first = None
    for index,d in enumerate(read(root/'data/dataset.json')):
        _,pool = unpack(d['state'])
        for pi,permutation in enumerate(d['permutations']):
            order = [pool[j] for j in permutation]
            slot = f'cache/{d["state"]["state_id"]}/{pi}'
            c,q,e = model.forward(order,slot)
            payload = dict(candidates=c.clone(),query=q.clone(),cache_key=e['cache_key'],
                           candidate_ids=[v.id for v in order],positions=e['encoded']['candidate_positions'])
            path = root/'cache/shards'/d['state']['state_id']/f'{pi}.pt'
            path.parent.mkdir(parents=True,exist_ok=True)
            start = time.perf_counter()
            with path.open('xb') as f:
                torch.save(payload,f)
            rows.append(dict(state_id=d['state']['state_id'],permutation=pi,cache_key=e['cache_key'],
                path=path.relative_to(root).as_posix(),sha256=sha(path),write_seconds=time.perf_counter()-start))
            if first is None:
                first = (order,c.clone(),q.clone(),e['cache_key'])
        print('cache',index+1,d['state']['state_id'],len(rows),flush=True)
    c,q,e = model.forward(first[0],'cache-check-fresh')
    if not torch.equal(c,first[1]) or not torch.equal(q,first[2]) or e['cache_key']!=first[3]:
        raise ValueError('cached hidden vectors differ from fresh actual forward')
    write(root/'cache/index.json',rows)
    write(root/'cache/verification.json',dict(fresh_equal=True,entries=len(rows),frozen=model.prove_frozen(),identity=model.identity))
    sampler.close()
    return dict(entries=len(rows),forward_count=journal.counts['forward'])


def validation(head,kind,groups):
    records = []
    with torch.no_grad():
        for d,samples in groups:
            c,q,target = samples[0]
            logits = head(c,q)
            scores = dict(zip(d['labels']['candidate_ids'],logits.tolist()))
            order = ranked_ids(scores)
            ids = d['labels']['candidate_ids']
            means = dict(zip(ids,d['labels']['q_ref']))
            online = dict(zip(ids,d['labels']['online']))
            selected = min(order[:2],key=lambda cid:(-math.fsum(online[cid][:4])/4,cid))
            prob = torch.softmax(logits,dim=-1)
            records.append(dict(state_id=d['state']['state_id'],scenario=d['state']['scenario'],
                scores=scores,ranking_ids=order,selected=selected,retained=order[:2],
                regret=max(means.values())-means[selected],screening_loss=max(means.values())-max(means[c] for c in order[:2]),
                soft_nll=-(target*torch.log_softmax(logits,dim=-1)).sum().item(),brier=((prob-target)**2).sum().item(),
                selected_position=ids.index(selected),top1_position=ids.index(order[0])))
    aggregate = {key:float(np.mean([r[key] for r in records])) for key in ('regret','screening_loss','soft_nll','brier')}
    return dict(aggregate=aggregate,by_scenario={scene:{key:float(np.mean([r[key] for r in records if r['scenario']==scene]))
                  for key in aggregate} for scene in ('balanced','scarce')},records=records)


def training_groups(root,kind):
    result = []
    for d in read(root/'data/dataset.json'):
        samples = []
        for pi,perm in enumerate(d['permutations']):
            if kind=='P':
                path = root/'cache/shards'/d['state']['state_id']/f'{pi}.pt'
                value = torch.load(path,map_location='cpu',weights_only=True)
                if value['candidate_ids'] != [d['labels']['candidate_ids'][i] for i in perm]:
                    raise ValueError('cache permutation alignment mismatch')
                c,q = value['candidates'],value['query']
            else:
                data = dict(quantized=[d['numeric']['quantized'][i] for i in perm])
                c,q = (torch.tensor(x) for x in structured(data))
            samples.append((c,q,torch.tensor([d['labels']['target'][i] for i in perm],dtype=torch.float32)))
        result.append((d,samples))
    return result


def train(root):
    root = Path(root)
    torch.set_num_threads(1)
    journal = Journal(root)
    index = read(root/'cache/index.json')
    for row in index:
        if sha(root/row['path'])!=row['sha256']:
            raise ValueError('training cache changed')
    results = []
    for kind in ('S','P'):
        groups = training_groups(root,kind)
        tr = [x for x in groups if x[0]['state']['split']=='train']
        val = [x for x in groups if x[0]['state']['split']=='validation']
        for seed in (11,29):
            started = time.perf_counter()
            torch.manual_seed(seed)
            rng = np.random.default_rng(seed)
            # Shuffle globally once, then reshuffle groups in every epoch.
            tr = [tr[i] for i in rng.permutation(len(tr))]
            head = SharedPointerHead(24,36) if kind=='S' else SharedPointerHead()
            opt = torch.optim.AdamW(head.parameters(),lr=.001,weight_decay=.01)
            initial = weight_digest(head)
            checkpoints = []
            updates = 0
            for epoch in range(21):
                if epoch:
                    order = rng.permutation(len(tr)).tolist()
                    for offset in range(0,len(order),8):
                        batch = [tr[i] for i in order[offset:offset+8]]
                        journal.reserve(kind+str(seed)+'_update',f'train/{kind}/{seed}/{updates+1}')
                        opt.zero_grad(set_to_none=True)
                        objective = torch.stack([loss(head,samples) for _,samples in batch]).sum()/8
                        objective.backward()
                        norm = torch.nn.utils.clip_grad_norm_(head.parameters(),1.)
                        if not torch.isfinite(objective) or not torch.isfinite(norm):
                            raise ValueError('nonfinite formal training loss/gradient')
                        opt.step()
                        updates += 1
                        row = dict(epoch=epoch,update=updates,loss=objective.item(),gradient_norm=norm.item(),
                            state_ids=[d['state']['state_id'] for d,_ in batch],group_weights=[1/8]*len(batch))
                        append(root/'training'/f'{kind}{seed}-steps.jsonl',row)
                        journal.finish(f'train/{kind}/{seed}/{updates}',row)
                if epoch in (0,2,5,10,15,20):
                    v = validation(head,kind,val)
                    path = root/'checkpoints'/f'{kind}{seed}'/f'epoch{epoch:02d}.pt'
                    receipt = save_checkpoint(path,head,opt,epoch=epoch,updates=updates,rng=rng,
                        extra=dict(kind=kind,seed=seed,data_sha256=sha(root/'data/dataset.json'),
                                   group_cursor=0,next_epoch=epoch+1))
                    reloaded,_ = load_head(path)
                    if validation(reloaded,kind,val) != v:
                        raise ValueError('checkpoint validation reload mismatch')
                    checkpoint = dict(epoch=epoch,updates=updates,receipt=receipt,validation=v)
                    write(root/'training'/f'{kind}{seed}-epoch{epoch:02d}.json',checkpoint)
                    checkpoints.append(checkpoint)
                    print('train',kind,seed,epoch,v['aggregate'],flush=True)
            best = min(checkpoints,key=lambda ck:(ck['validation']['aggregate']['regret'],
                       ck['validation']['aggregate']['screening_loss'],ck['validation']['aggregate']['soft_nll'],ck['epoch']))
            # Select step0 honestly if validation requests it, but still deliver all trained epochs.
            result = dict(kind=kind,seed=seed,selected_epoch=best['epoch'],selected_path=best['receipt']['path'],
                selected_sha256=best['receipt']['sha256'],initial_tensor_sha256=initial,
                final_tensor_sha256=weight_digest(head),parameters=sum(p.numel() for p in head.parameters()),
                training_seconds=time.perf_counter()-started,updates=updates,validation=best['validation']['aggregate'],
                checkpoint_bytes=sum(c['receipt']['bytes'] for c in checkpoints),
                checkpoint_seconds=sum(c['receipt']['seconds'] for c in checkpoints))
            if initial==weight_digest(head):
                raise ValueError('head failed to update')
            write(root/'models'/f'{kind}{seed}-selection.json',result)
            results.append(result)
    write(root/'training/summary.json',results)
    return results

"""Fresh process: verify checkpoint tensors/optimizer cursors and selected scores.

No optimizer step, Qwen load or DES is executed here.
"""
from pathlib import Path
import json
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
import torch
from scripts.phase2ec1_common import read,sha
from scripts.phase2ec1_training import training_groups,validation
from sim_jev.qwen_pointer_runtime import load_head,weight_digest


def audit(root):
    torch.set_num_threads(1)
    rows=[]
    for kind in ('P','S'):
        groups=training_groups(root,kind)
        val=[g for g in groups if g[0]['state']['split']=='validation']
        for seed in (11,29):
            selection=read(root/'models'/f'{kind}{seed}-selection.json')
            digests=[]
            for epoch in (0,2,5,10,15,20):
                path=root/'checkpoints'/f'{kind}{seed}'/f'epoch{epoch:02d}.pt'
                head,ck=load_head(path)
                assert ck['updates']==epoch*6 and ck['epoch']==epoch
                receipt=read(root/'training'/f'{kind}{seed}-epoch{epoch:02d}.json')
                assert receipt['receipt']['sha256']==sha(path)
                assert receipt['receipt']['tensor_sha256']==weight_digest(head)
                assert validation(head,kind,val)==receipt['validation']
                for entry in ck['optimizer']['state'].values():
                    assert int(entry['step'].item())==epoch*6
                digests.append(weight_digest(head))
            assert digests[0]!=digests[-1]
            path=root/'checkpoints'/f'{kind}{seed}'/f'epoch{selection["selected_epoch"]:02d}.pt'
            assert sha(path)==selection['selected_sha256']
            if seed==11:
                assert sha(root/'models'/f'{kind}11-selected.pt')==sha(path)
            rows.append(dict(kind=kind,seed=seed,selected_epoch=selection['selected_epoch'],
                             all_checkpoints_reload_exact=True,optimizer_cursors_verified=True,
                             actual_parameter_update=True))
    return dict(status='passed',rows=rows,new_updates=0,new_forwards=0,new_des=0)


if __name__=='__main__':
    if not __debug__:raise RuntimeError('optimized audit forbidden')
    print(json.dumps(audit(Path(sys.argv[1]))))

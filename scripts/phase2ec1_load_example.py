"""Load a trained P head and make one real, audited weekly decision.

This command is opt-in. It consumes the original campaign's remaining engineering
and forward allowance and never resets a budget. The state must be a legal saved
state JSON. Do not use this to rerun formal holdout decisions.
"""
from pathlib import Path
import argparse
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.phase2ec1_common import read,write,Journal
from scripts.phase2ec1_assets import DEST
from scripts.phase2eb import evaluate
from scripts.phase2d_local import unpack
from sim_jev.qwen_pointer_runtime import FrozenQwen,load_head
from sim_jev.qwen_pointer import CandidateHeadPolicySpec
from sim_jev.head_decision import HeadDecisionRuntime
from sim_jev.decision_resolver import resolve_decision


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--state-json',type=Path,required=True)
    args=parser.parse_args()
    state=read(args.state_json)
    req,pool=unpack(state)
    journal=Journal(args.root)
    slot='manual/'+str(time.time_ns())
    backbone=FrozenQwen(DEST,journal,args.root/slot/'load.json')
    head,checkpoint=load_head(args.root/'models/P11-selected.pt')
    runtime=HeadDecisionRuntime(backbone=backbone,p_head=head,s_head=None,a_model=None,
        evaluate=lambda request,pool,ids,b,seed:evaluate(args.root,journal,state,request,pool,ids,b,seed,'engineering_des',slot+'/branches'),
        evidence_writer=lambda row:write(args.root/slot/'selection-evidence.json',row),
        slot=slot,expected_pool=state['pool'])
    result=resolve_decision(req,CandidateHeadPolicySpec(planning_seed=read(args.root/'registration.json')['online_seed']),runtime)
    write(args.root/slot/'action.json',result.record)
    print(result.command.to_dict())
    print(result.record['continuous_wall_seconds'])


if __name__=='__main__':main()

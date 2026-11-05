"""Public offline R2 acceptance: strict historical audit, captures and fresh replay.

The evidence root is read-only. All receipts and replay outputs go into a new,
independent output directory. The fixed-SHA R1 parent archive binds the old
scientific source; the current R2 source executes every check and replay.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback
from unittest.mock import patch

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.phase2d_history import HistoricalR1Binding,COMPATIBILITY_VERSION,file_sha256
from scripts.phase2d_local import save,unpack
from scripts.phase2d_r1_data_audit import read
from sim_jev.local_provider import LocalProvider,validate_record
from sim_jev.planning import PlanningScenario
from sim_jev.provenance import canonical_sha256,code_fingerprint


def forbidden(*args,**kwargs):
    raise AssertionError('network/model/metadata request forbidden in offline R2 acceptance')


@contextmanager
def offline_evidence(evidence,*,allow_des=False,cost=None):
    """Reject writes to evidence and every network/model path in this process."""
    evidence=Path(evidence).resolve();active=[True]
    write_flags=os.O_WRONLY|os.O_RDWR|os.O_APPEND|os.O_CREAT|os.O_TRUNC
    def protected(value):
        return isinstance(value,(str,bytes,os.PathLike)) and Path(os.fsdecode(value)).resolve().is_relative_to(evidence)
    def guard(event,args):
        if not active[0]:return
        if event=='open' and isinstance(args[2],int) and args[2]&write_flags and protected(args[0]):
            raise AssertionError('evidence write forbidden: '+os.fsdecode(args[0]))
        if event in ('os.remove','os.rmdir','os.chmod','os.utime') and protected(args[0]):
            raise AssertionError('evidence mutation forbidden: '+event)
        if event in ('os.rename','os.replace') and any(protected(value) for value in args[:2]):
            raise AssertionError('evidence mutation forbidden: '+event)
        if event=='os.mkdir' and protected(args[0]):
            raise AssertionError('evidence directory mutation forbidden')
        if event.startswith('socket.'):
            raise AssertionError('socket operation forbidden: '+event)
    sys.addaudithook(guard)
    evaluate=PlanningScenario.evaluate
    def counted(instance,*args,**kwargs):
        if cost is not None:cost['new_des']+=1
        return evaluate(instance,*args,**kwargs)
    try:
        with patch.object(socket,'socket',side_effect=forbidden),\
             patch.object(socket,'create_connection',side_effect=forbidden),\
             patch.object(LocalProvider,'_http',side_effect=forbidden),\
             patch.object(LocalProvider,'verify_model',side_effect=forbidden),\
             patch.object(PlanningScenario,'evaluate',counted if allow_des else forbidden):
            yield
    finally:active[0]=False


def audit(evidence,output,binding,cost):
    from scripts.phase2d_r1_data_audit import verify
    result=verify(evidence,compatibility=binding,output=output/'data-audit.json')
    for key,expected in (('states',24),('unique_holdout_states',19),('actual_generations',141),
                         ('pilot_runs',60),('pilot_decisions',300),('formal_des',34048)):
        if result[key]!=expected:raise AssertionError('complete R1 audit count differs: '+key)
    if cost['new_des']:raise AssertionError('read-only scalar audit evaluated a new DES branch')
    return result


def captures(evidence,output,binding,cost):
    frozen=binding.frozen_for(evidence);results=[]
    template=read(evidence/'development/status.json')['selected_template']
    groups=(('legacy-v1',evidence/'legacy-v1/smoke'),('scores-v2',evidence/'development'/template))
    for label,folder in groups:
        paths=sorted(folder.glob('completion-*.json'))
        if len(paths)!=20:raise ValueError('expected 20 original real captures: '+label)
        for path in paths:
            original=read(path);canonical=original['rendered']['canonical']
            request,pool=unpack({'request':canonical['decision'],'pool':canonical['candidates']})
            ident=original['identity']
            if label=='scores-v2' and ident!=frozen['identity']:raise ValueError('v2 capture frozen identity differs')
            validate_record(original,request,pool,ident)
            if original['evidence']!='local_inference' or original['valid_local_model'] is not True:
                raise ValueError('capture is not original real local inference')
            destination=output/label/path.stem;store=destination/'responses'
            store.mkdir(parents=True)
            (store/(original['cache_key']+'.json')).write_bytes(path.read_bytes())
            provider=LocalProvider(ident,store,mode='cache_only');replayed=provider.rank(request,pool)
            stable=('protocol','identity','cache_key','rendered','ranked_ids','choice','score','probability',
                    'confidence','score_semantics','attempts')
            if label=='scores-v2':stable+=('preference_scores','ties','sort_rule')
            for key in stable:
                if canonical_sha256(replayed[key])!=canonical_sha256(original[key]):
                    raise AssertionError('captured cache field differs: '+key)
            if provider.generations or provider.metadata_queries:raise AssertionError('capture replay made new requests')
            save(destination/'replayed-record.json',replayed)
            results.append({'version':label,'capture':path.name,'capture_sha256':file_sha256(path),
                            'cache_key':original['cache_key'],'strict_parse_and_cache_read':True})
    return {'status':'passed','legacy_v1_captures':20,'scores_v2_captures':20,'results':results,
            'new_generations':0,'metadata_queries':0,'new_des':0,'live_inference_validated':False}


def replay(evidence,output,binding,cost,*,limit=None,probe_run=None):
    from sim_jev.branch_audit_store import BranchAuditStore
    from sim_jev.decision_resolver import DecisionRuntime,logical_decision_record
    from sim_jev.weekly import WeeklySimulation
    from sim_jev.weekly_reporting import write_weekly_run
    from scripts.phase2d_r1 import build_case,policy
    from scripts.weekly_verify import normalized_history,audit_business_log
    from scripts.planning_verify import audit_planning_branch
    from scripts.phase2c_science_regression import SCIENCE_FILES
    frozen=binding.frozen_for(evidence)
    if read(evidence/'pilot/status.json')['status']!='passed':raise ValueError('historical pilot incomplete')
    runs=sorted(path for path in (evidence/'pilot').iterdir() if path.is_dir() and (path/'result.json').is_file())
    if len(runs)!=60:raise ValueError('expected exactly 60 historical completed runs')
    selected=([run for run in runs if run.name==probe_run] if probe_run is not None else
              runs if limit is None else runs[:limit])
    if not selected:raise ValueError('replay selection must be nonempty')
    results=[];hours=0.;science=[];runtime=[]
    for run in selected:
        row=read(run/'result.json');expected=WeeklySimulation.from_checkpoint(read(run/'checkpoint.json'))
        destination=output/'runs'/run.name
        journal=BranchAuditStore(destination,audit_planning_branch)
        local=LocalProvider(frozen['identity'],run/'responses',mode='cache_only') if row['method'].startswith('local-') else None
        actual=build_case(row['scenario'],row['seed'],policy(row['method'],frozen['identity']),
                          DecisionRuntime(branch_auditor=journal,local_provider=local))
        actual.run(720);audit_business_log(actual)
        left,right=expected.to_checkpoint(),actual.to_checkpoint()
        for value,sim in ((left,expected),(right,actual)):
            value['history']=normalized_history(sim)
            if 'decision_records' in value['weekly_state']:
                value['weekly_state']['decision_records']=[logical_decision_record(record)
                    for record in value['weekly_state']['decision_records']]
        if left!=right or expected.summary()!=actual.summary() or expected.events!=actual.events:
            raise AssertionError('exact full checkpoint/business replay differs: '+run.name)
        counts=journal.reconcile(actual.to_checkpoint());save(destination/'branch-accounting.json',counts)
        write_weekly_run(actual,destination/'science')
        for name in SCIENCE_FILES:
            old=(run/'science'/name).read_bytes();new=(destination/'science'/name).read_bytes()
            identical=old==new
            science.append({'run':run.name,'file':name,'bytes':len(old),'identical':identical,
                'old_sha256':hashlib.sha256(old).hexdigest(),'new_sha256':hashlib.sha256(new).hexdigest()})
            save(output/'scientific-comparison.json',science)
            if not identical:raise AssertionError('scientific bytes differ: '+run.name+'/'+name)
        old_runtime=run/'science/runtime.json';new_runtime=destination/'science/runtime.json'
        runtime.append({'run':run.name,'scope':'provenance only; outside the fixed nine scientific files',
                        'old_sha256':file_sha256(old_runtime) if old_runtime.exists() else None,
                        'new_sha256':file_sha256(new_runtime) if new_runtime.exists() else None})
        if local and (local.generations or local.metadata_queries):raise AssertionError('cache replay made requests')
        hours+=sum(record['budget']['simulated_hours'] for record in actual.weekly_state.get('decision_records',[]))
        results.append({'run':run.name,'passed':True,'science_files_identical':len(SCIENCE_FILES),
            'new_generations':local.generations if local else 0,'metadata_queries':local.metadata_queries if local else 0,
            'all_five_decisions_replayed':True,**counts})
        save(output/'results.json',results)
        print('R2 historical cache replay',len(results),len(selected),run.name,flush=True)
    if sum(item['logical_branches'] for item in results)!=cost['new_des']:
        raise AssertionError('measured DES evaluations differ from persistent replay accounting')
    complete=limit is None and probe_run is None
    if complete and (len(results)!=60 or len(science)!=540 or cost['new_des']!=6400):
        raise AssertionError('full replay acceptance coverage differs')
    save(output/'runtime-provenance-comparison.json',runtime)
    return {'status':'passed' if complete else 'probe_passed','runs':len(results),
        'science_files_byte_identical':len(science),'cache_only_replay_verified':complete,
        'extra_audit_des':cost['new_des'],'extra_audit_simulated_hours':hours,
        'new_generations':0,'metadata_queries':0,'network_requests':0,
        'scientific_file_set':list(SCIENCE_FILES),'scientific_tolerance':0,
        'budget_role':'additional R2 audit replay, excluded from historical formal 34048 DES'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('audit','captures','replay'))
    parser.add_argument('--evidence-root',type=Path,required=True)
    parser.add_argument('--parent-archive',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--compatibility-version',default=COMPATIBILITY_VERSION)
    parser.add_argument('--limit',type=int,help='small replay probe only; never a complete acceptance')
    parser.add_argument('--probe-run',help='one exact historical run name; never a complete acceptance')
    args=parser.parse_args(argv)
    evidence=args.evidence_root.resolve();output=args.output.resolve()
    if output.is_relative_to(evidence) or evidence.is_relative_to(output):
        parser.error('output and read-only evidence must be independent directories')
    if args.limit is not None and (args.action!='replay' or not 1<=args.limit<=60):
        parser.error('--limit is allowed only for a replay probe between 1 and 60 runs')
    if args.probe_run is not None and (args.action!='replay' or args.limit is not None):
        parser.error('--probe-run is allowed only for replay and cannot be combined with --limit')
    output.mkdir(parents=True,exist_ok=False)
    started=time.perf_counter();source=code_fingerprint(ROOT)
    cost={'new_des':0,'new_model_generations':0,'new_metadata_requests':0,'new_http_requests':0}
    receipt={'schema':'phase2d-r2-offline-command-v1','argv':sys.orig_argv if argv is None else argv,
        'cwd':str(Path.cwd()),'interpreter':sys.executable,'python_version':sys.version,
        'evidence_root':str(evidence),'output':str(output),'execution_source':source,'cost':cost}
    try:
        with offline_evidence(evidence,allow_des=args.action=='replay',cost=cost):
            binding=HistoricalR1Binding(evidence,args.parent_archive,version=args.compatibility_version)
            receipt['binding']=binding.identity()
            result=(replay(evidence,output,binding,cost,limit=args.limit,probe_run=args.probe_run) if args.action=='replay' else
                    globals()[args.action](evidence,output,binding,cost))
        if code_fingerprint(ROOT)!=source:raise ValueError('execution source changed during acceptance')
        receipt.update(exit_code=0,status='passed',result=result)
    except Exception as error:
        receipt.update(exit_code=1,status='failed',error=f'{type(error).__name__}: {error}',traceback=traceback.format_exc())
        print(receipt['traceback'],file=sys.stderr)
    receipt['seconds']=time.perf_counter()-started
    save(output/'command.json',receipt)
    save(output/'status.json',{key:value for key,value in receipt.items() if key not in ('execution_source','traceback')})
    print(json.dumps({key:value for key,value in receipt.items() if key not in ('execution_source','traceback')},indent=2))
    return receipt['exit_code']


if __name__=='__main__':raise SystemExit(main())

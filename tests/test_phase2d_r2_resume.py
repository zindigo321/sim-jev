"""Immutable run preflight exercised through the real runner and new processes."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import phase2d_local as local
from scripts import phase2d_r1 as r1
from sim_jev.branch_audit_store import BranchAuditStore, read_json
from sim_jev.local_provider import LocalProvider
from sim_jev.planning import PlanningScenario
from sim_jev.provenance import canonical_sha256
from sim_jev.weekly import WeeklySimulation
from scripts.planning_verify import audit_planning_branch
from tests.test_phase2d_r1_scores import score_identity, scores_fixture

ROOT = Path(__file__).resolve().parents[1]


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


def cli(folder, *options):
    argv = [sys.executable, str(ROOT/'scripts/phase2d_local.py'), 'recovery-run',
            '--output', str(folder), *map(str, options)]
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding='utf-8')


class ImmutableRunIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root/'run'

    def tearDown(self):
        self.temp.cleanup()

    def start(self, **kwargs):
        return local.run_pilot_case(self.run, 'balanced', 30, 'mandatory-first', None,
                                    until=31.3, **kwargs)

    def assert_rejected_without_writes(self, invoke, pattern='identity|historical|exogenous'):
        before = tree_bytes(self.run)
        with patch.object(PlanningScenario, 'evaluate', side_effect=AssertionError('no new DES')), \
                patch.object(WeeklySimulation, 'run', side_effect=AssertionError('no forward execution')), \
                patch.object(socket, 'socket', side_effect=AssertionError('no HTTP')), \
                self.assertRaisesRegex(ValueError, pattern):
            invoke()
        self.assertEqual(tree_bytes(self.run), before)

    def test_scenario_seed_and_method_mismatch_precede_every_write(self):
        self.start()
        for name, seed, method in [('scarce',30,'mandatory-first'),
                ('balanced',31,'mandatory-first'), ('balanced',30,'random-des')]:
            with self.subTest(name=name,seed=seed,method=method):
                self.assert_rejected_without_writes(lambda: local.run_pilot_case(
                    self.run,name,seed,method,None,until=720))

    def test_full_policy_same_method_is_checked(self):
        def small_policy(method,ident):
            return replace(local.policy(method,ident),top_k=1,scenarios_per_candidate=1)
        local.run_pilot_case(self.run,'balanced',30,'heuristic-des',None,
            until=0,policy_fn=small_policy)
        def changed_policy(method,ident):
            return replace(small_policy(method,ident),planning_seed=20261103)
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'heuristic-des',None,until=168,policy_fn=changed_policy))

    def test_full_local_model_and_renderer_identity_are_checked(self):
        ident=score_identity()
        provider=LocalProvider(ident,mode='fixture',transport=scores_fixture)
        local.run_pilot_case(self.run,'balanced',30,'local-direct',ident,
            until=0,policy_fn=r1.policy,build_case_fn=r1.build_case,local_provider=provider)
        self.assertEqual(provider.generations,0)
        model=deepcopy(ident)
        model['model_digest']='sha256:'+'1'*64
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'local-direct',model,until=168,
            policy_fn=r1.policy,build_case_fn=r1.build_case))
        # A supported renderer identity change is a policy change; no cache
        # lookup or model repair is allowed before this gate rejects it.
        from sim_jev.local_provider import CANONICAL_RENDERER, validate_identity
        canonical=deepcopy(ident);canonical['renderer']=CANONICAL_RENDERER
        validate_identity(canonical)
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'local-direct',canonical,until=168,
            policy_fn=r1.policy,build_case_fn=r1.build_case))

    def test_freeze_identity_is_full_content_bound(self):
        context={'version':'offline-test-freeze','source':{'sha256':'a'*64},
                 'config':{'x':[1,2,3]},'identity':{'model':'explicit-offline-fixture'}}
        self.start(experiment_context=context)
        altered=deepcopy(context);altered['config']['x'][1]=4
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'mandatory-first',None,until=168,experiment_context=altered))
        roundtrip=json.loads(json.dumps(context,sort_keys=True))
        row,_=local.run_pilot_case(self.run,'balanced',30,'mandatory-first',None,
                                  until=47.125,experiment_context=roundtrip)
        self.assertEqual(row['time'],47.125)

    def test_changed_config_rule_and_task_content_are_checked(self):
        self.start()
        def changed_config(name,seed,policy):
            sim=local.build_case(name,seed,policy)
            sim.config=replace(sim.config,initial_spares=sim.config.initial_spares+1)
            return sim
        def changed_rule(name,seed,policy):
            sim=local.build_case(name,seed,policy)
            sim.weekly_state['rules_input'][0]['min_reserve']+=1
            return sim
        def changed_task(name,seed,policy):
            sim=local.build_case(name,seed,policy)
            values=list(sim._input_missions)
            values[0]=replace(values[0],value=values[0].value+1)
            sim._input_missions=tuple(values)
            return sim
        for builder in (changed_config,changed_rule,changed_task):
            with self.subTest(builder=builder.__name__):
                self.assert_rejected_without_writes(lambda: local.run_pilot_case(
                    self.run,'balanced',30,'mandatory-first',None,until=168,build_case_fn=builder))

    def test_checkpoint_identity_and_exogenous_record_are_both_checked(self):
        self.start()
        path=self.run/'exogenous.json'
        exogenous=read_json(path);exogenous['missions'][0]['value']+=1
        local.save(path,exogenous)
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'mandatory-first',None,until=168))

    def test_historical_partial_lacking_source_and_freeze_binding_is_rejected(self):
        self.start()
        (self.run/'run-identity.json').unlink()
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'mandatory-first',None,until=168),'historical.*independent|historical.*Independently')

    def test_orphan_historical_output_cannot_be_initialized_as_a_new_run(self):
        self.run.mkdir()
        for name in ('exogenous.json','resume-point.json','science.csv','unknown.txt'):
            with self.subTest(name=name):
                path=self.run/name;path.write_text('{}',encoding='utf-8')
                self.assert_rejected_without_writes(lambda: local.run_pilot_case(
                    self.run,'balanced',30,'mandatory-first',None,until=168),'historical')
                path.unlink()

    def test_pre_checkpoint_exogenous_tampering_is_rejected_before_attempt_write(self):
        self.start()
        (self.run/'checkpoint.json').unlink()
        path=self.run/'exogenous.json';value=read_json(path)
        value['rules'][0]['min_reserve']+=1;local.save(path,value)
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'mandatory-first',None,until=168),'exogenous')

    def test_unknown_identity_version_and_rehashed_source_change_are_rejected(self):
        self.start()
        path=self.run/'run-identity.json';original=read_json(path)
        unknown=deepcopy(original);unknown['version']='unknown'
        local.save(path,unknown)
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'mandatory-first',None,until=168),'unknown')
        altered=deepcopy(original);altered['inputs']['execution_source_sha256']='0'*64
        altered['identity_sha256']=canonical_sha256(altered['inputs'])
        local.save(path,altered)
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'mandatory-first',None,until=168))

    def test_completed_return_is_read_only_and_checks_full_identity(self):
        local.run_pilot_case(self.run,'balanced',30,'mandatory-first',None)
        before=tree_bytes(self.run)
        with patch.object(WeeklySimulation,'run',side_effect=AssertionError('no repeat')), \
                patch.object(PlanningScenario,'evaluate',side_effect=AssertionError('no DES')):
            for _ in range(2):
                row,_=local.run_pilot_case(self.run,'balanced',30,'mandatory-first',None)
                self.assertEqual(row['actual_des'],0)
        self.assertEqual(before,tree_bytes(self.run))
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'scarce',30,'mandatory-first',None))
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'mandatory-first',None,experiment_context={'other':'freeze'}))

    def test_completed_same_method_full_strategy_mismatch_is_read_only(self):
        def small_policy(method,ident):
            return replace(local.policy(method,ident),top_k=1,scenarios_per_candidate=1)
        local.run_pilot_case(self.run,'balanced',30,'heuristic-des',None,policy_fn=small_policy)
        before=tree_bytes(self.run)
        with patch.object(PlanningScenario,'evaluate',side_effect=AssertionError('no repeat')):
            row,_=local.run_pilot_case(self.run,'balanced',30,'heuristic-des',None,policy_fn=small_policy)
        self.assertEqual(row['actual_des'],5)
        self.assertEqual(before,tree_bytes(self.run))
        def changed_policy(method,ident):
            return replace(small_policy(method,ident),scenarios_per_candidate=2)
        self.assert_rejected_without_writes(lambda: local.run_pilot_case(
            self.run,'balanced',30,'heuristic-des',None,policy_fn=changed_policy))

    def test_read_only_journal_refuses_missing_empty_topology_and_pending_repair(self):
        self.start()
        missing=self.run/'branch-audit-store/artifacts';missing.rmdir()
        with self.assertRaisesRegex(ValueError,'missing durable branch audit directory'):
            BranchAuditStore(self.run,audit_planning_branch,read_only=True)
        self.assertFalse(missing.exists())
        pending=self.root/'pending'
        def small_policy(method,ident):
            return replace(local.policy(method,ident),top_k=1,scenarios_per_candidate=1)
        local.run_pilot_case(pending,'balanced',30,'heuristic-des',None,
            until=0,policy_fn=small_policy)
        attempt_path=next((pending/'branch-audit-store/attempts').glob('*.json'))
        attempt=read_json(attempt_path);attempt['status']='pending';local.save(attempt_path,attempt)
        before=tree_bytes(pending)
        journal=BranchAuditStore(pending,audit_planning_branch,read_only=True)
        with self.assertRaisesRegex(ValueError,'pending/uncertain'):
            journal.verify_checkpoint(pending/'checkpoint.json')
        self.assertEqual(before,tree_bytes(pending))

    def test_same_identity_json_and_new_process_continuation(self):
        first=cli(self.run,'--method','mandatory-first','--until',31.3)
        self.assertEqual(first.returncode,0,first.stdout+first.stderr)
        manifest=read_json(self.run/'run-identity.json')
        local.save(self.run/'run-identity.json',json.loads(json.dumps(manifest,sort_keys=True)))
        second=cli(self.run,'--method','mandatory-first','--until',720)
        self.assertEqual(second.returncode,0,second.stdout+second.stderr)
        continuous=self.root/'continuous'
        final=cli(continuous,'--method','mandatory-first','--until',720)
        self.assertEqual(final.returncode,0,final.stdout+final.stderr)
        left=WeeklySimulation.from_checkpoint(read_json(continuous/'checkpoint.json'))
        right=WeeklySimulation.from_checkpoint(read_json(self.run/'checkpoint.json'))
        local.compare_replay(left,right)
        before=tree_bytes(self.run)
        idempotent=cli(self.run,'--method','mandatory-first','--until',720)
        self.assertEqual(idempotent.returncode,0,idempotent.stdout+idempotent.stderr)
        self.assertEqual(before,tree_bytes(self.run))

    def test_cli_mismatch_nonzero_receipt_outside_run_and_no_mutation(self):
        first=cli(self.run,'--method','mandatory-first','--until',168)
        self.assertEqual(first.returncode,0,first.stdout+first.stderr)
        variants=[['--scenario','scarce'],['--seed',31],['--method','random-des']]
        for options in variants:
            before=tree_bytes(self.run)
            invocation=cli(self.run,'--method','mandatory-first','--until',720,*options)
            self.assertNotEqual(invocation.returncode,0)
            self.assertIn('run identity mismatch',invocation.stderr)
            self.assertEqual(before,tree_bytes(self.run))
        self.assertTrue(list((self.root/'run-receipts').glob('*.json')))
        self.assertFalse((self.run/'commands').exists())

    def test_cli_cannot_write_rejection_receipt_inside_the_run(self):
        invocation=cli(self.run,'--method','mandatory-first','--until',0,
                       '--receipt-output',self.run/'commands')
        self.assertEqual(invocation.returncode,2)
        self.assertIn('outside the immutable run',invocation.stderr)
        self.assertFalse(self.run.exists())


if __name__=='__main__':
    unittest.main()

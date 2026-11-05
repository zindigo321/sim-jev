"""Exact pause recovery. Synthetic response controls are never model evidence."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.phase2c_experiment import build_case
from scripts.phase2d_local import save
from scripts.weekly_verify import normalized_history
from sim_jev.decision import DecisionPolicySpec, DECISION_PROTOCOL_VERSION_V2
from sim_jev.decision_resolver import DecisionRuntime, logical_decision_record
from sim_jev.local_policy import LocalDecisionPolicySpec, LOCAL_SCORE_POLICY_VERSION
from sim_jev.local_provider import LocalProvider, GenerationBudget, identity_v2, render_for_identity
from sim_jev.versions import ARITHMETIC_PROTOCOL_LEGACY, ARITHMETIC_PROTOCOL_V2
from sim_jev.weekly import WeeklySimulation
from sim_jev.weekly_reporting import write_weekly_run
from sim_jev.weekly_policy import PolicySpec, RuleSpec, WeeklyCommand, choose_command

STOPS = (31.3, 47.125, 95.75, 191.375, 335.2, 511.625, 671.875)


def offline_identity():
    return identity_v2({'digest': 'sha256:' + 'a' * 64,
                        'version': {'version': 'OFFLINE-FAKE-CACHE-CONTROL'},
                        'runtime_sha256': 'b' * 64, 'show': {'template': 'offline test'}},
                       tokenizer_sha256='c' * 64,
                       tokenizer_runtime={'kind': 'offline-token-count-control'})


class OfflineReferenceCacheControl(LocalProvider):
    """Drive the production cache writer with explicitly synthetic test output."""
    def verify_model(self):
        pass

    def rank(self, legal, pool, **kwargs):
        reference = choose_command(legal.observation, legal.context, PolicySpec())
        selected = next(candidate.id for candidate in pool if candidate.command == reference)
        mapping = render_for_identity(legal, pool, self.identity)['candidate_mapping']
        self.first = next(short for short, full in mapping.items() if full == selected)
        return super().rank(legal, pool, **kwargs)

    def _generate(self, body, timeout):
        keys = body['format']['properties']['preference_scores']['required']
        return {'model': self.identity['model'], 'done': True, 'done_reason': 'stop',
                'response': json.dumps({'preference_scores': {
                    key: 100 if key == self.first else 0 for key in keys}}),
                'prompt_eval_count': 100, 'eval_count': 10}


def case(method, *, protocol=ARITHMETIC_PROTOCOL_V2, runtime=None, scenario='scarce', seed=13):
    if method == 'mandatory-first':
        spec = PolicySpec()
    elif method.startswith('local-'):
        spec = LocalDecisionPolicySpec(method=method, identity=offline_identity(),
                                       version=LOCAL_SCORE_POLICY_VERSION)
    else:
        spec = DecisionPolicySpec(method=method)
    old = build_case(scenario, seed, spec, runtime)
    return WeeklySimulation(old.config, old._input_missions,
                            scenario_id=old.weekly_state['scenario_id'],
                            rules=[RuleSpec.from_dict(row) for row in old.weekly_state['rules_input']],
                            policy=spec, decision_runtime=runtime, arithmetic_protocol=protocol)


def logical_checkpoint(sim):
    value = sim.to_checkpoint()
    # A pause emits one observational history row. The established comparator
    # removes only unchanged-state history rows, never any requests or metrics.
    value['history'] = normalized_history(sim)
    state = value['weekly_state']
    if 'decision_records' in state:
        state['decision_records'] = [logical_decision_record(row) for row in state['decision_records']]
    return value


def worker(source, destination, target, store):
    def forbidden(*args, **kwargs):
        raise AssertionError('network forbidden during cache-only recovery')
    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket, 'create_connection', forbidden):
        sim = WeeklySimulation.load_checkpoint(source)
        provider = None
        if sim.weekly_state['policy_spec']['kind'] == 'local-candidate-des':
            provider = OfflineReferenceCacheControl(offline_identity(), Path(store), mode='cache_only')
            sim._decision_runtime = DecisionRuntime(local_provider=provider)
        sim.run(float(target))
        save(Path(destination), sim.to_checkpoint())
        if provider is not None:
            assert provider.generations == provider.metadata_queries == 0


class PauseIdentityTests(unittest.TestCase):
    def assertExact(self, whole, restored):
        self.assertEqual(whole.events, restored.events)
        self.assertEqual(whole.get_observation(), restored.get_observation())
        self.assertEqual(whole.summary(), restored.summary())
        self.assertEqual(logical_checkpoint(whole), logical_checkpoint(restored))

    def test_all_baselines_multiple_ordinary_pauses_and_json_restores(self):
        for method in ('mandatory-first', 'random-des', 'heuristic-des', 'direct-des'):
            with self.subTest(method=method):
                whole, paused, restored = case(method), case(method), case(method)
                whole.run(720)
                for stop in STOPS:
                    paused.run(stop)
                    restored.run(stop)
                    restored = WeeklySimulation.from_checkpoint(json.loads(json.dumps(restored.to_checkpoint(), allow_nan=False)))
                paused.run(720)
                restored.run(720)
                self.assertExact(whole, paused)
                self.assertExact(whole, restored)

    def test_v2_local_methods_new_process_cache_only_multiple_recovery(self):
        for method in ('local-direct', 'local-des'):
            with self.subTest(method=method), tempfile.TemporaryDirectory(prefix='OFFLINE-NOT-MODEL-') as directory:
                folder = Path(directory)
                live = OfflineReferenceCacheControl(offline_identity(), folder / 'responses', mode='live',
                            budget=GenerationBudget(folder / 'synthetic-budget.json'), token_counter=lambda _: 100)
                whole = case(method, runtime=DecisionRuntime(local_provider=live))
                whole.run(720)
                self.assertEqual(live.generations, 5)
                cache = OfflineReferenceCacheControl(offline_identity(), folder / 'responses', mode='cache_only')
                paused = case(method, runtime=DecisionRuntime(local_provider=cache))
                paused.run(STOPS[0])
                source = folder / 'checkpoint-0.json'
                save(source, paused.to_checkpoint())
                for index, stop in enumerate((*STOPS[1:], 720), 1):
                    destination = folder / f'checkpoint-{index}.json'
                    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker',
                                             str(source), str(destination), str(stop), str(folder / 'responses')],
                                            cwd=ROOT, capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    source = destination
                restored = WeeklySimulation.load_checkpoint(source)
                self.assertEqual(cache.generations, 0)
                self.assertEqual(cache.metadata_queries, 0)
                self.assertExact(whole, restored)
                records = restored.weekly_state['decision_records']
                self.assertTrue(all(row['request']['protocol_version'] == DECISION_PROTOCOL_VERSION_V2 for row in records))

    def test_anchor_corruption_unknown_protocol_and_silent_downgrade_rejected(self):
        sim = case('mandatory-first'); sim.run(31.3)
        original = sim.to_checkpoint()
        self.assertEqual(original['schema_version'], 2)
        mutations = [lambda value: value.pop('accumulation_anchor'),
                     lambda value: value.update(arithmetic_protocol='unknown'),
                     lambda value: value.update(schema_version=1),
                     lambda value: value['accumulation_anchor'].update(time=31.3),
                     lambda value: value['accumulation_anchor']['metrics'].update(flight_hours=0.),
                     lambda value: value['accumulation_anchor']['aircraft'].update(extra=0.)]
        for mutation in mutations:
            value = deepcopy(original); mutation(value)
            with self.assertRaises(ValueError):
                WeeklySimulation.from_checkpoint(value)

    def test_old_checkpoint_load_remains_explicit_legacy(self):
        old = case('mandatory-first', protocol=ARITHMETIC_PROTOCOL_LEGACY)
        old.run(31.3)
        value = old.to_checkpoint()
        self.assertEqual(value['schema_version'], 1)
        self.assertNotIn('accumulation_anchor', value)
        restored = WeeklySimulation.from_checkpoint(json.loads(json.dumps(value)))
        self.assertEqual(restored.arithmetic_protocol, ARITHMETIC_PROTOCOL_LEGACY)
        restored.run(720); restored.to_checkpoint()

    def test_reporting_checkpoint_preserves_public_order_for_both_protocols(self):
        for protocol in (ARITHMETIC_PROTOCOL_LEGACY, ARITHMETIC_PROTOCOL_V2):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                continuous = case('heuristic-des', protocol=protocol, scenario='balanced')
                continuous.run(168)
                write_weekly_run(continuous, Path(directory) / 'science')
                restored = WeeklySimulation.load_checkpoint(Path(directory) / 'science/checkpoint.json')
                self.assertEqual(continuous.get_observation(), restored.get_observation())
                self.assertEqual(list(continuous.weekly_state['commitments']),
                                 list(restored.weekly_state['commitments']))
                continuous.run(336); restored.run(336)
                self.assertExact(continuous, restored)

    def test_frozen_commands_preserve_physical_and_random_realization_across_versions(self):
        for scenario in ('balanced', 'scarce'):
            legacy = case('mandatory-first', protocol=ARITHMETIC_PROTOCOL_LEGACY, scenario=scenario)
            legacy.run(720)
            fixed = PolicySpec('scripted', 'frozen-command-cross-version',
                               tuple(WeeklyCommand.from_dict(row) for row in legacy.weekly_state['weekly_commands']))
            source = build_case(scenario, 13, fixed)
            anchored = WeeklySimulation(source.config, source._input_missions,
                    scenario_id=source.weekly_state['scenario_id'],
                    rules=[RuleSpec.from_dict(row) for row in source.weekly_state['rules_input']],
                    policy=fixed, arithmetic_protocol=ARITHMETIC_PROTOCOL_V2)
            anchored.run(720)
            self.assertEqual(legacy.events, anchored.events)
            self.assertEqual(legacy.keyed_rng.to_dict(), anchored.keyed_rng.to_dict())
            self.assertEqual(legacy.weekly_state['cohorts'], anchored.weekly_state['cohorts'])
            self.assertEqual([asdict(row) for row in legacy.orders.values()],
                             [asdict(row) for row in anchored.orders.values()])
            anchored.to_checkpoint()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker(*sys.argv[2:])
    else:
        unittest.main()

"""Synthetic boundary tests for the frozen diagnostic; no new environments."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from scripts.phase2e_analysis import decompose, paired_intervals
from scripts.phase2e_diagnostic import (DESJournal, check_registration, deduplicate,
    preregister, request_pool_identity, require_g1, capture, seed_usage, read, main)
from sim_jev.provenance import canonical_sha256


def public_row(name='one', literal='business'):
    return {'state_id': name, 'request': {'request_id': 'wrapper-' + name, 'protocol_version': 'version',
        'observation': {'mission': literal, 'values': [1, 2.]}, 'context': {'window': 336},
        'parameters': {'fleet': 4}}, 'pool': [{'id': 'candidate-wrapper-' + name,
        'command': {'accepted_ids': ['M01']}, 'summary': {'schema_version': 'v1',
        'generator_version': 'v1', 'business': literal}}]}


class DiagnosticRegistrationTests(unittest.TestCase):
    def test_business_projection_strips_only_listed_wrappers(self):
        first = public_row()
        other = public_row('two')
        self.assertNotEqual(request_pool_identity(first['request'], first['pool'])[0], request_pool_identity(other['request'], other['pool'])[0])
        self.assertEqual(request_pool_identity(first['request'], first['pool'])[1], request_pool_identity(other['request'], other['pool'])[1])
        for change in ('array', 'numeric', 'literal'):
            bad = deepcopy(first)
            if change == 'array':
                bad['request']['observation']['values'].reverse()
            elif change == 'numeric':
                bad['request']['observation']['values'][0] = 1.
            else:
                bad['pool'][0]['summary']['business'] = 'different'
            self.assertNotEqual(request_pool_identity(first['request'], first['pool'])[1], request_pool_identity(bad['request'], bad['pool'])[1])

    def test_cross_seen_and_within_business_duplicates_use_first_plan(self):
        seen_row = public_row('seen', 'seen')
        exact, business = request_pool_identity(seen_row['request'], seen_row['pool'])
        seen = [{'exact_input_sha256': exact, 'business_input_sha256': business}]
        first = public_row('first', 'new')
        duplicate = public_row('later', 'new')
        result = deduplicate([seen_row, first, duplicate], seen)
        self.assertEqual([row['primary_eligible'] for row in result], [False, True, False])
        self.assertTrue(result[2]['within_batch_business_duplicate'])
        self.assertFalse(result[2]['within_batch_exact_duplicate'])
        self.assertEqual(result[2]['business_representative_state_id'], 'first')

    def test_conflicting_seed_replacement_is_frozen_before_any_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen = public_row()
            source = {'sha256': 'source', 'files': []}
            result = preregister(root, {'renderer': 'v4'}, [seen],
                [{'scenario': 'balanced', 'seed': 1000, 'path': 'old.json'}], source)
            self.assertEqual(result['planned_inputs'], 40)
            self.assertEqual(result['new_states_built'], 0)
            registered = read(root / 'diagnostic-registration.json')
            self.assertEqual(registered['planned_inputs'][0]['environment_seed'], 3000)
            self.assertEqual(registered['planned_inputs'][20]['environment_seed'], 2000)
            self.assertFalse((root / 'diagnostics').exists())
            with patch('scripts.phase2e_diagnostic.code_fingerprint', return_value=source):
                self.assertEqual(check_registration(root), registered)
                (root / 'diagnostic-seed-usage.json').write_text('[]', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'seed usage'):
                    check_registration(root)

    def test_capture_refuses_incomplete_g1_before_building_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = {'sha256': 'source', 'files': []}
            preregister(root, {'renderer': 'v4'}, [public_row()], [], source)
            (root / 'development').mkdir()
            (root / 'development/status.json').write_text(json.dumps({'status': 'partial'}), encoding='utf-8')
            with patch('scripts.phase2e_diagnostic.code_fingerprint', return_value=source), patch('scripts.phase2e_diagnostic.build_case') as build:
                with self.assertRaisesRegex(ValueError, 'G1'):
                    capture(root)
                build.assert_not_called()
            self.assertFalse((root / 'diagnostics').exists())

    def test_gate_binds_new_renderer_source_identity_and_no_uncertainty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'development').mkdir()
            registration = {'source': {'sha256': 'source'}, 'identity_sha256': 'identity'}
            good = {'status': 'passed', 'valid_completions': 20, 'unique_states': 8,
                    'pending_uncertain': 0, 'source_sha256': 'source', 'identity_sha256': 'identity'}
            for field, value in ((None, None), ('pending_uncertain', 1), ('identity_sha256', 'v3'), ('valid_completions', 19)):
                changed = deepcopy(good)
                if field:
                    changed[field] = value
                (root / 'development/status.json').write_text(json.dumps(changed), encoding='utf-8')
                if field is None:
                    self.assertEqual(require_g1(root, registration), good)
                else:
                    with self.assertRaises(ValueError):
                        require_g1(root, registration)

    def test_seed_scan_reads_metadata_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'result.json').write_text(json.dumps({'scenario': 'balanced', 'seed': 1001}), encoding='utf-8')
            (root / 'branch-audit-store').mkdir()
            (root / 'branch-audit-store/result.json').write_text(json.dumps({'seed': 9999}), encoding='utf-8')
            usage = seed_usage([root])
            self.assertEqual([(row['scenario'], row['seed']) for row in usage], [('balanced', 1001)])

    def test_reference_cli_dispatches_only_after_explicit_command(self):
        with patch('scripts.phase2e_diagnostic.reference', return_value={'states': 40}) as run, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['reference', '--root', 'evidence']), 0)
            run.assert_called_once_with(Path('evidence'))

    def test_preregister_cli_reads_fixed_sources_without_building_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = {'sha256': 'source'}
            (root / 'freeze.json').write_text(json.dumps({'identity': {'renderer': 'v4'}, 'source': source}), encoding='utf-8')
            with patch('scripts.phase2e_diagnostic.code_fingerprint', return_value=source), \
                 patch('scripts.phase2e_diagnostic.collect_seen_public', return_value=[public_row()]) as seen, \
                 patch('scripts.phase2e_diagnostic.seed_usage', return_value=[]) as usage, \
                 patch('scripts.phase2e_diagnostic.build_case') as build, patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(main(['preregister', '--root', str(root), '--seen-evidence', 'fixed-R1', '--history-root', 'old-runs']), 0)
                seen.assert_called_once_with(Path('fixed-R1'))
                usage.assert_called_once_with([Path('old-runs')])
                build.assert_not_called()
            self.assertEqual(len(read(root / 'diagnostic-registration.json')['planned_inputs']), 40)
            self.assertFalse((root / 'diagnostics').exists())


class FormalDESBudgetTests(unittest.TestCase):
    def test_reservation_survives_restart_and_blocks_unknown_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'journal.jsonl'
            request = {'legal': 1}
            candidate = type('Candidate', (), {'id': 'C'})()
            scenario = type('Scenario', (), {'scenario_id': 'S'})()
            ledger = DESJournal(path, 'registration')
            key = ledger.reserve('online', 'state/method', request, candidate, scenario)
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                DESJournal(path, 'registration').reserve('online', 'new', request, candidate, scenario)
            ledger.finish(key, 'audit-identity')
            restored = DESJournal(path, 'registration')
            self.assertEqual(restored.summary()['online_attempts'], 1)
            self.assertEqual(restored.summary()['pending_uncertain'], 0)
            with self.assertRaisesRegex(ValueError, 'already reserved'):
                restored.reserve('online', 'state/method', request, candidate, scenario)
            with self.assertRaisesRegex(ValueError, 'identity'):
                DESJournal(path, 'different-registration')

    def test_hard_limits_count_reservations_before_computation(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = DESJournal(Path(temporary) / 'journal.jsonl', 'registration')
            candidate = type('Candidate', (), {'id': 'C'})()
            scenario = type('Scenario', (), {'scenario_id': 'S'})()
            with patch('scripts.phase2e_diagnostic.DES_LIMITS', {'online': 1, 'reference': 1}):
                key = ledger.reserve('online', 'one', {}, candidate, scenario)
                ledger.finish(key, 'audit')
                with self.assertRaisesRegex(ValueError, 'hard limit'):
                    ledger.reserve('online', 'two', {}, candidate, scenario)


class FrozenPairedAnalysisTests(unittest.TestCase):
    def test_two_loss_terms_equal_final_regret_and_no_fake_direct_topk(self):
        scores = {'a': [1.] * 64, 'b': [.8] * 64, 'c': [.4] * 64}
        actual = decompose(scores, 'c', ['b', 'c'])
        self.assertAlmostEqual(actual['screening_loss'], .2)
        self.assertAlmostEqual(actual['within_retained_selection_loss'], .4)
        self.assertAlmostEqual(actual['final_action_regret'], .6)
        direct = decompose(scores, 'c', None)
        self.assertIsNone(direct['screening_loss'])
        self.assertIsNone(direct['within_retained_selection_loss'])
        self.assertAlmostEqual(direct['final_action_regret'], .6)

    def test_invalid_reference_count_and_retained_membership_fail(self):
        with self.assertRaisesRegex(ValueError, '64'):
            decompose({'a': [0.]}, 'a', None)
        with self.assertRaisesRegex(ValueError, 'preselection'):
            decompose({'a': [0.] * 64, 'b': [1.] * 64}, 'a', ['b'])

    def test_paired_intervals_are_deterministic_keep_crn_and_separate_sources(self):
        differences = [[.02] * 64, [.04] * 64]
        a = paired_intervals(differences, scene_index=0, repetitions=500)
        b = paired_intervals(differences, scene_index=0, repetitions=500)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a['mean_improvement'], .03)
        self.assertAlmostEqual(a['paired_reference_mc_se'], 0.)
        self.assertTrue(np.allclose(a['conditional_reference_mc_95_interval'], [.03, .03]))
        self.assertTrue(np.allclose(a['environment_paired_95_interval'], [.02, .04]))
        paired = paired_intervals([[0., .02] * 32], scene_index=1, repetitions=500)
        self.assertGreater(paired['paired_reference_mc_se'], 0)
        self.assertEqual(paired['environment_paired_95_interval'], [.01, .01])

    def test_missing_independent_environments_returns_no_invented_interval(self):
        result = paired_intervals([], scene_index=0)
        self.assertEqual(result['n'], 0)
        self.assertIsNone(result['environment_paired_95_interval'])


if __name__ == '__main__':
    unittest.main()

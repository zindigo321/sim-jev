"""Regression checks for loss attribution from real retained sets."""
from copy import deepcopy
import unittest

from scripts.phase2e_r1_decompose import business_projection, decompose, identity, paired, strict_json


class R1DecompositionTests(unittest.TestCase):
    def setUp(self):
        self.state = {'state_id': 'fixture', 'scenario': 'balanced', 'seed': 1,
                      'window_start': 336, 'window_end': 504}
        self.scores = {'a': [1.] * 64, 'b': [.8] * 64, 'c': [.3] * 64,
                       'd': [.2] * 64, 'e': [.1] * 64}

    def test_actual_retained_set_decomposes_regret(self):
        action = {'method': 'heuristic-des', 'selected_candidate_id': 'c',
                  'actual_des': 32, 'status': 'completed', 'top_k': ['b', 'c', 'd', 'e']}
        record = {'method': action['method'], 'selected_candidate_id': 'c', 'ranking_ids': ['b', 'c', 'd', 'e', 'a'],
                  'budget': {'actual_des': 32}, 'branches': [{'candidate_id': c} for c in ['b', 'c', 'd', 'e'] for _ in range(8)]}
        row = decompose(self.state, action, record, self.scores)
        self.assertAlmostEqual(row['screening_loss'], .2)
        self.assertAlmostEqual(row['within_retained_selection_loss'], .5)
        self.assertAlmostEqual(row['final_action_regret'], .7)
        forged = deepcopy(record)
        forged['branches'][0]['candidate_id'] = 'a'
        with self.assertRaisesRegex(ValueError, 'evaluated set'):
            decompose(self.state, action, forged, self.scores)

    def test_local_direct_exported_top_k_is_not_a_retained_set(self):
        action = {'method': 'local-direct', 'selected_candidate_id': 'b',
                  'actual_des': 0, 'status': 'completed', 'top_k': ['b', 'a', 'c']}
        record = {'method': action['method'], 'selected_candidate_id': 'b',
                  'budget': {'actual_des': 0}, 'branches': [], 'ranking_ids': ['b', 'a', 'c', 'd', 'e']}
        row = decompose(self.state, action, record, self.scores)
        self.assertIsNone(row['screening_loss'])
        self.assertIsNone(row['within_retained_selection_loss'])
        self.assertIsNone(row['actual_prefilter_ids'])
        self.assertEqual(row['local_direct_action'], 'b')

    def test_pairing_uses_common_scenario_indices(self):
        a = [i / 64 for i in range(64)]
        b = [v - .1 for v in a]
        self.assertAlmostEqual(paired(a, b)['paired_difference_se'], 0., places=15)
        self.assertGreater(paired(a, list(reversed(b)))['paired_difference_se'], .01)

    def test_business_projection_removes_only_identity_and_keeps_numeric_types(self):
        a = {'request': {'request_id': 'x', 'protocol_version': 'v1', 'observation': {'n': 1}},
             'pool': [{'id': 'ca', 'summary': {'literal': 'ca'}, 'command': {'ids': ['x', 'y']}}]}
        b = deepcopy(a)
        b['request']['request_id'] = 'other'
        b['pool'][0]['id'] = 'cb'
        self.assertEqual(identity(business_projection(a)), identity(business_projection(b)))
        b['request']['observation']['n'] = 1.
        self.assertNotEqual(identity(business_projection(a)), identity(business_projection(b)))
        b = deepcopy(a)
        b['pool'][0]['summary']['literal'] = 'cb'
        self.assertNotEqual(identity(business_projection(a)), identity(business_projection(b)))

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self):
        for payload in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(ValueError):
                strict_json(payload)


if __name__ == '__main__':
    unittest.main()

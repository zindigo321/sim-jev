"""Negative phase gates use explicit offline fixtures, never model evidence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import phase2d_r1 as r1
from sim_jev.candidates import generate_candidates
from sim_jev.decision import DecisionRequest
from sim_jev.local_provider import GenerationBudget, LocalError, LocalProvider
from sim_jev.provenance import canonical_sha256, code_fingerprint
from tests.test_phase2d_local import captured, test_identity
from tests.test_phase2d_r1_scores import score_identity, scores_fixture


class FormalGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old, _ = captured()
        cls.req = DecisionRequest.create(old.observation, old.context, old.parameters,
                                        protocol_version='legal-weekly-request-v2')
        cls.pool = generate_candidates(cls.req, 16, 20261102)
        cls.ident = score_identity()
        cls.template = cls.ident['template']

    def make_gate_inputs(self, root):
        # This deliberately insufficient evidence must never unlock real work.
        rows = [{'state_id': f'{name}-{seed}-{hour}', 'request': self.req.to_dict(),
                 'pool': [c.to_dict() for c in self.pool], 'actual_M': len(self.pool),
                 'business_request_sha256': canonical_sha256(r1.business_projection(self.req.to_dict())),
                 'independent_identity': canonical_sha256([self.req.to_dict(), [c.to_dict() for c in self.pool]])}
                for name in ('balanced', 'scarce') for seed in (10, 11) for hour in (168, 672)]
        r1.save(root / 'development/status.json', {'status': 'passed', 'all_development_states_valid': True,
            'new_live_stability_verified': True, 'selected_template': self.template})
        r1.save(root / 'development-identity.json', self.ident)
        r1.save(root / 'development/states.json', rows)
        r1.save(root / 'development/registration.json', {'source_sha256': code_fingerprint(r1.ROOT)['sha256'],
            'templates': [self.template]})
        r1.save(root / 'development/identities.json', {self.template: self.ident})
        return rows

    def test_missing_or_failed_engineering_stops_before_state_capture(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(r1, 'capture_states', side_effect=AssertionError('state capture forbidden')):
                with self.assertRaises(FileNotFoundError):
                    r1.development(root)
                gate = {key: True for key in r1.ENGINEERING_GATES}
                gate['pause_identity_verified'] = False
                r1.save(root / 'engineering-gates.json', gate)
                with self.assertRaisesRegex(ValueError, 'engineering gates incomplete'):
                    r1.development(root)
            self.assertFalse((root / 'development/states.json').exists())

    def test_stale_engineering_source_cannot_unlock(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            r1.save(root / 'engineering-gates.json', {**{key: True for key in r1.ENGINEERING_GATES},
                                                   'source_sha256': '0' * 64})
            with self.assertRaisesRegex(ValueError, 'source changed'):
                r1.require_engineering(root)

    def test_old_v1_identity_cannot_unlock_v2_from_passed_status(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_gate_inputs(root)
            r1.save(root / 'development-identity.json', test_identity())
            with self.assertRaisesRegex(ValueError, 'v1 cannot unlock'):
                r1.validate_development_gate(root)

    def test_handwritten_status_without_twenty_completions_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_gate_inputs(root)
            with self.assertRaises(FileNotFoundError), patch.object(r1, 'capture_states', side_effect=AssertionError('no new states')):
                r1.validate_development_gate(root)
            self.assertFalse((root / 'freeze.json').exists())

    def test_fixture_response_cannot_count_as_real_stability(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_gate_inputs(root)
            result = LocalProvider(self.ident, mode='fixture', transport=scores_fixture).rank(self.req, self.pool)
            r1.save(root / 'development' / self.template / 'completion-00.json', result)
            with self.assertRaisesRegex((ValueError, LocalError), 'cache/fixture cannot count|cache_identity_mismatch'):
                r1.validate_development_gate(root)

    def test_uncertain_attempt_blocks_acceptance_without_refund(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.make_gate_inputs(root)
            budget = GenerationBudget(root / 'generation-budget.json')
            budget.reserve('development', 'explicit-offline-test-reservation')
            before = budget.read()
            with self.assertRaisesRegex(ValueError, 'uncertain generation'):
                r1.validate_development_gate(root)
            self.assertEqual(before, budget.read())

    def test_missing_difficult_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rows = self.make_gate_inputs(root)
            r1.save(root / 'development/states.json', [r for r in rows if r['state_id'] != 'scarce-10-168'])
            with self.assertRaisesRegex(ValueError, 'all eight'):
                r1.validate_development_gate(root)

    def test_strict_diagnostic_failure_does_not_create_reference_labels(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rows = self.make_gate_inputs(root)
            # No actual holdout state is generated. The failure is injected into
            # the real diagnostics orchestration with an explicit seen fixture.
            class Failure:
                def rank(self, request, pool):
                    raise LocalError('invalid_local_scores', {'evidence': 'offline gate fixture'})
            with patch.object(r1, 'check_freeze', return_value={'identity': self.ident}), \
                    patch.object(r1, 'capture_states', return_value=deepcopy(rows[:1])), \
                    patch.object(r1, 'provider', return_value=Failure()):
                with self.assertRaisesRegex(LocalError, 'invalid_local_scores'):
                    r1.diagnostics(root)
                self.assertFalse((root / 'diagnostics/online-complete.json').exists())
                self.assertFalse((root / 'diagnostics/evaluation').exists())
                with patch.object(r1.PlanningScenario, 'evaluate', side_effect=AssertionError('reference DES forbidden')), \
                        self.assertRaises(FileNotFoundError):
                    r1.reference(root)
                self.assertFalse((root / 'diagnostics/evaluation').exists())


if __name__ == '__main__':
    unittest.main()

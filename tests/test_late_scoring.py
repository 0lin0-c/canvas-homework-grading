import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from qwen_grader import apply_score
from yxin_grader import reusable_judgment


class LateScoringTests(unittest.TestCase):
    def test_rounded_up_day_boundaries_and_floor(self):
        for seconds, errors, expected, days in [
            (0, 0, 100, 0), (1, 0, 99.5, 1), (86399, 0, 99.5, 1),
            (86400, 0, 99.5, 1), (86401, 0, 99, 2),
            (172799, 0, 99, 2), (172800, 0, 99, 2), (259200, 2, 96.5, 3),
            (864000, 0, 95, 10), (950400, 0, 95, 11), (86400, 6, 95, 1),
            (777600, 1, 95, 9),
        ]:
            with self.subTest(seconds=seconds, errors=errors):
                item = {'questions': [{'status': 'incorrect'}] * errors}
                apply_score(item, {'late': seconds > 0, 'seconds_late': seconds})
                self.assertEqual(item['proposed_score'], expected)
                self.assertEqual(item['late_days'], days)
                self.assertEqual(item['late_deduction'], max(95, 100 - errors) - expected)

    def test_unknown_or_invalid_lateness_requires_review(self):
        for seconds in (None, -1, '86400', True, float('nan'), float('inf')):
            with self.subTest(seconds=seconds):
                item = {'questions': []}
                apply_score(item, {'late': True, 'seconds_late': seconds})
                self.assertIsNone(item['proposed_score'])
                self.assertIsNone(item['late_days'])

    def test_mismatch_still_has_no_score(self):
        item = {'questions': []}
        apply_score(item, {'late': True, 'seconds_late': 86400}, mismatch=True)
        self.assertIsNone(item['proposed_score'])

    def test_changed_lateness_cannot_reuse_old_score(self):
        student = {'source_fingerprint': 'same-upload', 'late': True, 'seconds_late': 86400}
        old = dict(student, questions=[])
        self.assertTrue(reusable_judgment(old, student, [], Path('.')))
        student['seconds_late'] = 172800
        self.assertFalse(reusable_judgment(old, student, [], Path('.')))

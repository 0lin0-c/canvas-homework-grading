import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from assignment_store import assignment_catalog, batch_path, load_batch
from qwen_grader import GradingError, read_json, save_json

A = 'https://oc.sjtu.edu.cn/courses/94139/assignments/428317'
B = 'https://oc.sjtu.edu.cn/courses/94139/assignments/430062'


class AssignmentStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_legacy_migration_preserves_original_and_separates_assignments(self):
        legacy = self.root / 'local' / 'batch-review.json'
        save_json(legacy, {'assignment_url': A, 'students': [{'id': '1', 'proposed_score': 99}]})
        original = legacy.read_bytes()
        self.assertIsNone(load_batch(self.root, B, 'review'))
        self.assertEqual(load_batch(self.root, A, 'review'), read_json(legacy))
        save_json(batch_path(self.root, B, 'review'), {'assignment_url': B, 'students': []})
        self.assertEqual(load_batch(self.root, A, 'review')['students'][0]['proposed_score'], 99)
        self.assertEqual(load_batch(self.root, B, 'review')['students'], [])
        self.assertEqual(legacy.read_bytes(), original)

    def test_saved_identity_must_match_and_invalid_url_is_rejected(self):
        save_json(batch_path(self.root, A, 'review'), {'assignment_url': B})
        with self.assertRaises(GradingError):
            load_batch(self.root, A, 'review')
        with self.assertRaises(GradingError):
            batch_path(self.root, 'https://oc.sjtu.edu.cn/courses/../assignments/2', 'source')

    def test_catalog_uses_assignment_name_and_can_fall_back_to_cache(self):
        class Client:
            def read(self, *_):
                return [{'id': 428317, 'name': '9/16作业', 'due_at': '2026-09-22T15:59:59Z'},
                        {'id': 430062, 'name': '9/23作业', 'due_at': '2026-09-29T15:59:59Z'}]
        result = assignment_catalog(Client(), self.root)
        self.assertEqual([x['title'] for x in result['assignments']], ['9/16作业', '9/23作业'])
        class Offline:
            def read(self, *_):
                raise GradingError('离线')
        cached = assignment_catalog(Offline(), self.root)
        self.assertEqual(cached['assignments'], result['assignments'])
        self.assertIn('缓存', cached['warning'])

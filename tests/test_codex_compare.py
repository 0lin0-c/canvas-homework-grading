import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from codex_compare import FILES, publish
from qwen_grader import read_json, save_json


class CodexComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'local' / 'run-one'
        self.folder.mkdir(parents=True)
        Image.new('RGB', (100, 100), 'white').save(self.root / 'sample.png')
        batch = {'assignment_url': 'https://example.test/assignment', 'title': '虚构样本',
                 'question_numbers': [1], 'reference_materials': {'questions': {'1': {
                     kind: [{'path': 'sample.png', 'source': kind}]
                     for kind in ('question_images', 'answer_images')}}},
                 'students': [{'id': 's1', 'name': '虚构学生', 'submitted_at': 'test',
                               'attachments': [{'id': 'a1', 'page_count': 1}],
                               'pages': [{'id': 'p1', 'attachment_id': 'a1', 'page': 1,
                                          'path': 'sample.png'}]}]}
        save_json(self.root / 'local' / 'batch-source.json', batch)
        for role, (filename, model) in FILES.items():
            status = 'incorrect' if role in ('luna', 'terra', 'sol56') else 'correct'
            reason = '公式错误' if status == 'incorrect' else ''
            save_json(self.folder / filename, {'model': model, 'assignment_url': batch['assignment_url'],
                'students': [{'id': 's1', 'name': '虚构学生', 'submission_mismatch': False,
                              'reviewed_pages': ['p1'], 'questions': [{'number': 1, 'status': status,
                                  'reason': reason, 'evidence': [{'page_id': 'p1', 'bbox': [0, 0, .5, .5]}]
                                  if reason else []}]}]})

    def test_publish_uses_reviewed_result_and_preserves_previous(self):
        save_json(self.root / 'local' / 'batch-review.json', {'prior': True})
        result = publish(self.folder, self.root)
        student = result['students'][0]
        self.assertEqual(student['questions'][0]['status'], 'uncertain')
        self.assertEqual(student['proposed_score'], 100)
        self.assertEqual(student['model_comparison']['5.6']['questions'][0]['status'], 'incorrect')
        self.assertTrue((self.root / student['questions'][0]['images'][0]['path']).is_file())
        self.assertEqual(read_json(self.folder / 'previous-batch-review.json'), {'prior': True})
        self.assertEqual(read_json(self.root / 'local' / 'batch-review.json'), result)

    def test_missing_review_keeps_previous(self):
        save_json(self.root / 'local' / 'batch-review.json', {'prior': True})
        (self.folder / FILES['sol6'][0]).unlink()
        with self.assertRaises(FileNotFoundError):
            publish(self.folder, self.root)
        self.assertEqual(read_json(self.root / 'local' / 'batch-review.json'), {'prior': True})


if __name__ == '__main__':
    unittest.main()

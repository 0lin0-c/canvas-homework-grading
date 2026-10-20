import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from qwen_grader import (GradingError, grade_batch, load_settings, make_payload,
                         read_json, save_json, validate_result)


class GraderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        Image.new('RGB', (100, 100), 'white').save(self.root / 'page.png')
        refs = {str(n): {kind: [{'path': 'page.png', 'source': 'official'}]
                         for kind in ('question_images', 'answer_images')} for n in (1, 2)}
        self.student = {'id': 'sample', 'name': '虚构样本', 'submitted_at': 'test',
                        'attachments': [{'id': 'a', 'page_count': 1}],
                        'pages': [{'id': 'p1', 'attachment_id': 'a', 'page': 1, 'path': 'page.png'}]}
        self.batch = {'assignment_url': 'https://example.test/assignment', 'title': '测试',
                      'question_numbers': [1, 2], 'reference_materials': {'questions': refs},
                      'students': [self.student]}
        self.result = {'reviewed_pages': ['p1'], 'submission_mismatch': False,
                       'questions': [{'number': 1, 'status': 'incorrect', 'reason': '计算错误',
                                      'evidence': [{'page_id': 'p1', 'bbox': [0, 0, .5, .5]}]},
                                     {'number': 2, 'status': 'uncertain', 'reason': '疑似漏答，请核对',
                                      'evidence': [{'page_id': 'p1', 'bbox': [.5, .5, 1, 1]}]}]}

    def response(self, payload, key, base):
        self.assertEqual(payload['model'], 'qwen3.8-max')
        return {'id': 'fake', 'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(self.result)}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
                          'prompt_tokens_details': {'cached_tokens': 30}}}

    def test_mock_complete_flow_score_crops_and_usage(self):
        result = grade_batch(self.batch, self.root, 'fake', 'unused', self.response)
        self.assertEqual(result['students'][0]['proposed_score'], 99)
        self.assertEqual(result['qwen_usage']['totals']['total_tokens'], 120)
        self.assertTrue(result['students'][0]['comment'].startswith('1.'))
        self.assertEqual(read_json(self.root/'local/batch-review.json'), result)
        for q in result['students'][0]['questions']:
            self.assertTrue((self.root/q['images'][0]['path']).is_file())

    def test_failure_preserves_old_results_and_received_usage(self):
        save_json(self.root/'local/batch-review.json', {'old': True})
        self.result['questions'][0]['evidence'][0]['bbox'] = [0, 0, 2, 1]
        with self.assertRaises(GradingError):
            grade_batch(self.batch, self.root, 'fake', 'unused', self.response)
        self.assertEqual(read_json(self.root/'local/batch-review.json'), {'old': True})
        ledger = read_json(next((self.root/'local/qwen-runs').glob('*/usage.json')))
        self.assertEqual(ledger['state'], 'failed')
        self.assertEqual(ledger['calls'][0]['usage']['total_tokens'], 120)

    def test_late_penalty_combines_with_question_deduction(self):
        self.student.update(late=True, seconds_late=259200)
        result = grade_batch(self.batch, self.root, 'fake', 'unused', self.response)
        student = result['students'][0]
        self.assertEqual(student['proposed_score'], 97.5)
        self.assertEqual(student['late_days'], 3)
        self.assertEqual(student['late_deduction'], 1.5)

    def test_missing_and_duplicate_questions_rejected(self):
        for questions in [self.result['questions'][:1], [self.result['questions'][0]] * 2]:
            data = dict(self.result, questions=questions)
            with self.assertRaises(GradingError):
                validate_result(data, [1, 2], self.student['pages'])

    def test_unread_pages_rejected(self):
        data = dict(self.result, reviewed_pages=[])
        with self.assertRaises(GradingError):
            validate_result(data, [1, 2], self.student['pages'])

    def test_incomplete_input_fails_before_call(self):
        self.student['attachments'][0]['page_count'] = 2
        with self.assertRaises(GradingError):
            grade_batch(self.batch, self.root, '', '', lambda *args: self.fail('network called'))

    def test_no_old_grades_in_payload(self):
        self.student.update(comment='SECRET_OLD_COMMENT', proposed_score=97)
        payload = make_payload(self.batch, self.student, self.root)
        self.assertNotIn('SECRET_OLD_COMMENT', json.dumps(payload))

    def test_missing_key_and_nonofficial_endpoint(self):
        with patch.dict('os.environ', {'DASHSCOPE_API_KEY': '', 'QWEN_BASE_URL': ''}):
            with self.assertRaises(GradingError):
                load_settings(self.root)
        with patch.dict('os.environ', {'DASHSCOPE_API_KEY': 'fake', 'QWEN_BASE_URL': 'https://attacker.test/compatible-mode/v1'}):
            with self.assertRaises(GradingError):
                load_settings(self.root)

    def test_mismatch_is_not_treated_as_a_wrong_answer(self):
        self.result['submission_mismatch'] = True
        result = grade_batch(self.batch, self.root, 'fake', 'unused', self.response)
        self.assertIsNone(result['students'][0]['proposed_score'])
        self.assertTrue(all(q['status'] == 'uncertain' for q in result['students'][0]['questions']))


if __name__ == '__main__':
    unittest.main()

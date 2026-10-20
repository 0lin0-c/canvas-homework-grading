import json
import io
import base64
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from qwen_grader import GradingError, read_json, save_json
from yxin_grader import call_yxin, compact_image_content, grade_batch, load_settings, model_payload, request_summary


class TwoPassTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        Image.new('RGB', (100, 100), 'white').save(self.root / 'page.png')
        refs = {str(number): {kind: [{'path': 'page.png', 'source': 'reference'}]
                              for kind in ('question_images', 'answer_images')}
                for number in (1, 2)}
        self.batch = {'assignment_url': 'https://example.test/assignment', 'title': '虚构样本',
                      'question_numbers': [1, 2], 'reference_materials': {'questions': refs},
                      'students': [{'id': 's1', 'name': '虚构学生', 'submitted_at': 'test',
                                    'attachments': [{'id': 'a1', 'page_count': 1}],
                                    'pages': [{'id': 'p1', 'attachment_id': 'a1', 'page': 1,
                                               'path': 'page.png'}]}]}
        self.pairs = [('5.6', 'gpt-5.6-terra', 'gpt-5.6-sol')]
        self.names = {model for _, grader, reviewer in self.pairs for model in (grader, reviewer)}

    def test_two_pass_result_disagreement_and_usage(self):
        self.batch['students'][0].update(late=True, seconds_late=259200)
        responses = [
            [('incorrect', '计算错误'), ('correct', '')],
            [('correct', ''), ('correct', '')],
        ]
        models = []
        def fake_transport(payload, key, endpoint):
            models.append(payload['model'])
            self.assertNotIn('enable_thinking', payload)
            self.assertEqual(len(payload['messages']), 2)
            statuses = responses[len(models) - 1]
            questions = [{'number': number, 'status': status, 'reason': reason,
                          'evidence': [{'page_id': 'p1', 'bbox': [0, 0, .5, .5]}] if status != 'correct' else []}
                         for number, (status, reason) in enumerate(statuses, 1)]
            return {'id': str(len(models)), 'choices': [{'finish_reason': 'stop',
                    'message': {'content': json.dumps({'reviewed_pages': ['p1'],
                        'submission_mismatch': False, 'questions': questions})}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}
        result = grade_batch(self.batch, self.root, ('key', 'https://yxai.chat/v1', self.pairs),
                             transport=fake_transport, catalog=self.names)
        self.assertEqual(models, ['gpt-5.6-terra', 'gpt-5.6-sol'])
        student = result['students'][0]
        self.assertEqual([q['status'] for q in student['questions']], ['uncertain', 'correct'])
        self.assertEqual(student['proposed_score'], 98.5)
        self.assertEqual(student['late_days'], 3)
        self.assertEqual(student['late_deduction'], 1.5)
        self.assertEqual(student['comment'], '')  # Model disagreement stays in the TA review notes.
        self.assertTrue((self.root / student['questions'][0]['images'][0]['path']).is_file())
        self.assertEqual(result['yxin_usage']['totals']['total_tokens'], 240)
        self.assertEqual(list(student['model_comparison']), ['5.6'])
        self.assertEqual(read_json(self.root / 'local' / 'batch-review.json'), result)

    def test_unavailable_model_fails_before_images_sent(self):
        save_json(self.root / 'local' / 'batch-review.json', {'old': True})
        with self.assertRaisesRegex(GradingError, 'gpt-5.6-terra'):
            grade_batch(self.batch, self.root, ('key', 'https://yxai.chat/v1', self.pairs),
                        transport=lambda *args: self.fail('student data sent'),
                        catalog={'gpt-5.6-sol'})
        self.assertEqual(read_json(self.root / 'local' / 'batch-review.json'), {'old': True})

    def test_assignment_specific_result_does_not_overwrite_legacy_and_is_reusable(self):
        self.batch['students'][0]['source_fingerprint'] = 'version-1'
        legacy = self.root / 'local' / 'batch-review.json'
        save_json(legacy, {'old_assignment': True})
        path = self.root / 'local' / 'assignments' / 'current' / 'batch-review.json'
        def answer(*_):
            data = {'reviewed_pages': ['p1'], 'submission_mismatch': False,
                    'questions': [{'number': n, 'status': 'correct', 'reason': '', 'evidence': []}
                                  for n in (1, 2)]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(data)}}]}
        settings = ('key', 'https://yxai.chat/v1', self.pairs)
        result = grade_batch(self.batch, self.root, settings, transport=answer,
                             catalog=self.names, output_path=path)
        self.assertEqual(read_json(path), result)
        self.assertEqual(read_json(legacy), {'old_assignment': True})
        again = grade_batch(self.batch, self.root, settings,
                            transport=lambda *_: self.fail('unchanged answer sent again'),
                            catalog=set(), output_path=path)
        self.assertTrue(again['run_summary']['no_change'])

    def test_incremental_run_only_calls_models_for_new_or_changed_students(self):
        self.batch['students'][0]['source_fingerprint'] = 'version-1'
        calls = []
        def answer(payload, *_):
            calls.append(payload['model'])
            labels = [part['text'] for part in payload['messages'][1]['content']
                      if part.get('type') == 'text']
            page_id = 'p2' if any('学生页面ID：p2' in label for label in labels) else 'p1'
            data = {'reviewed_pages': [page_id], 'submission_mismatch': False,
                    'questions': [{'number': n, 'status': 'correct', 'reason': '', 'evidence': []}
                                  for n in (1, 2)]}
            return {'id': str(len(calls)), 'choices': [{'finish_reason': 'stop',
                    'message': {'content': json.dumps(data)}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}
        settings = ('key', 'https://yxai.chat/v1', self.pairs)
        first = grade_batch(self.batch, self.root, settings, transport=answer, catalog=self.names)
        self.assertEqual(len(calls), 2)
        again = grade_batch(self.batch, self.root, settings,
                            transport=lambda *_: self.fail('unchanged student sent to model'), catalog=set())
        self.assertEqual(again['run_summary'], {'graded_count': 0, 'reused_count': 1, 'no_change': True})
        self.assertEqual(len(calls), 2)

        newcomer = {'id': 's2', 'name': '新交学生', 'submitted_at': 'later',
                    'source_fingerprint': 'version-new',
                    'attachments': [{'id': 'a2', 'page_count': 1}],
                    'pages': [{'id': 'p2', 'attachment_id': 'a2', 'page': 1, 'path': 'page.png'}]}
        self.batch['students'].append(newcomer)
        second = grade_batch(self.batch, self.root, settings, transport=answer, catalog=self.names)
        self.assertEqual(len(calls), 4)
        self.assertEqual(second['run_summary']['graded_count'], 1)
        self.assertEqual(second['run_summary']['reused_count'], 1)
        self.assertEqual(second['students'][0]['review_revision'], first['students'][0]['review_revision'])
        self.assertNotEqual(second['students'][1]['review_revision'], first['students'][0]['review_revision'])
        self.assertEqual(len(list((self.root / 'local' / 'history').glob('*.json'))), 1)

        self.batch['students'][0]['source_fingerprint'] = 'version-2'
        metadata_only = grade_batch(self.batch, self.root, settings,
                                    transport=lambda *_: self.fail('identical pages regraded'), catalog=set())
        self.assertEqual(metadata_only['run_summary']['graded_count'], 0)
        self.assertEqual(read_json(self.root / 'local/batch-review.json')['students'][0]['source_fingerprint'], 'version-2')
        self.batch['students'][0]['source_fingerprint'] = 'version-3'
        self.batch['students'][0]['submitted_at'] = 'resubmitted'
        third = grade_batch(self.batch, self.root, settings, transport=answer, catalog=self.names)
        self.assertEqual(len(calls), 6)
        self.assertEqual(third['run_summary']['graded_count'], 1)
        self.assertEqual(third['students'][1]['review_revision'], second['students'][1]['review_revision'])

        newer_pairs = [('5.6', 'gpt-5.6-terra', 'gpt-6-sol')]
        newer = grade_batch(self.batch, self.root, ('key', 'https://yxai.chat/v1', newer_pairs),
                            transport=answer, catalog={'gpt-5.6-terra', 'gpt-6-sol'})
        self.assertEqual(len(calls), 10)
        self.assertEqual(newer['run_summary']['graded_count'], 2)

    def test_changed_page_content_cannot_reuse_metadata_only_result(self):
        from yxin_grader import submission_content_fingerprint, reusable_judgment
        student = self.batch['students'][0]
        student['source_fingerprint'] = 'old'
        old = dict(student, content_fingerprint=submission_content_fingerprint(student, self.root),
                   questions=[{'number': 1}, {'number': 2}])
        student['source_fingerprint'] = 'metadata-change'
        self.assertTrue(reusable_judgment(old, student, [1, 2], self.root))
        Image.new('RGB', (100, 100), 'black').save(self.root / 'page.png')
        self.assertFalse(reusable_judgment(old, student, [1, 2], self.root))

    def test_empty_provider_response_is_recorded_with_known_usage_and_preserves_result(self):
        path = self.root / 'local/batch-review.json'
        save_json(path, {'students': [], 'review_revision': 'old'})
        before = path.read_bytes()
        calls = []
        def empty(*_):
            calls.append(1)
            return {'choices': None, 'usage': {'prompt_tokens': 1919, 'completion_tokens': 0, 'total_tokens': 1919}}
        with self.assertRaisesRegex(GradingError, '空模型响应') as caught:
            grade_batch(self.batch, self.root, ('key', 'https://yxai.chat/v1', self.pairs),
                        transport=empty, catalog=self.names)
        self.assertEqual(calls, [1])
        self.assertNotIn('用量未知', str(caught.exception))
        self.assertEqual(path.read_bytes(), before)
        usage = read_json(next((self.root / 'local/yxin-runs').glob('*/usage.json')))
        self.assertEqual(usage['failure']['student_id'], 's1')
        self.assertTrue(usage['failure']['error']['response_invalid'])
        self.assertEqual(usage['returned_usage']['total_tokens'], 1919)

    def test_old_comparison_is_archived_after_first_full_run(self):
        old = {'review_revision': 'codex-old', 'codex_run': True,
               'students': [{'id': 's1', 'name': '虚构学生'}]}
        save_json(self.root / 'local' / 'batch-review.json', old)
        def correct(payload, *_):
            data = {'reviewed_pages': ['p1'], 'submission_mismatch': False,
                    'questions': [{'number': n, 'status': 'correct', 'reason': '', 'evidence': []}
                                  for n in (1, 2)]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(data)}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        grade_batch(self.batch, self.root, ('key', 'https://yxai.chat/v1', self.pairs),
                    transport=correct, catalog=self.names)
        archived = list((self.root / 'local' / 'history').glob('*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(read_json(archived[0]), old)

    def test_interrupted_run_reuses_completed_students_only(self):
        self.batch['students'][0]['source_fingerprint'] = 'version-1'
        self.batch['students'].append({'id': 's2', 'name': '新交学生', 'submitted_at': 'later',
                                       'source_fingerprint': 'version-2',
                                       'attachments': [{'id': 'a2', 'page_count': 1}],
                                       'pages': [{'id': 'p2', 'attachment_id': 'a2',
                                                  'page': 1, 'path': 'page.png'}]})
        calls = []
        def answer(payload, *_):
            calls.append(payload['model'])
            if len(calls) == 3:
                raise GradingError('模拟第二位学生请求失败')
            labels = [p['text'] for p in payload['messages'][1]['content'] if p.get('type') == 'text']
            page_id = 'p2' if any('学生页面ID：p2' in label for label in labels) else 'p1'
            data = {'reviewed_pages': [page_id], 'submission_mismatch': False,
                    'questions': [{'number': n, 'status': 'correct', 'reason': '', 'evidence': []}
                                  for n in (1, 2)]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(data)}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        settings = ('key', 'https://yxai.chat/v1', self.pairs)
        with self.assertRaisesRegex(GradingError, '第二位'):
            grade_batch(self.batch, self.root, settings, transport=answer, catalog=self.names)
        self.assertEqual(len(calls), 3)
        failed = read_json(next((self.root / 'local' / 'yxin-runs').glob('*/usage.json')))
        self.assertEqual(failed['returned_calls'], 2)
        self.assertEqual(failed['returned_usage']['total_tokens'], 4)
        calls.clear()
        result = grade_batch(self.batch, self.root, settings, transport=answer, catalog=self.names)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['run_summary'], {'graded_count': 1, 'reused_count': 1, 'no_change': False})
        self.assertEqual(len(result['students']), 2)

    def test_large_scan_uses_smaller_jpeg_model_input(self):
        path = self.root / 'photo.png'
        Image.effect_noise((1600, 1600), 80).convert('RGB').save(path)
        parts = compact_image_content(self.root, 'photo.png', 'test')
        self.assertTrue(parts[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
        self.assertLess(len(parts[1]['image_url']['url']), path.stat().st_size * 4 // 3)

    def test_oversized_request_resizes_images_without_dropping_pages_or_changing_sources(self):
        path = self.root / 'photo.png'
        Image.effect_noise((1200, 1600), 80).convert('RGB').save(path)
        before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        self.batch['students'][0]['pages'] = [
            {'id': f'p{n}', 'attachment_id': f'a{n}', 'page': 1, 'path': 'photo.png'} for n in (1, 2)]
        student = self.batch['students'][0]
        with patch('yxin_grader.MAX_REQUEST_BYTES', 100 * 1024 * 1024):
            original = model_payload(self.batch, student, self.root, 'test', 'grade')
        with patch('yxin_grader.MAX_REQUEST_BYTES', 1024 * 1024), patch('yxin_grader.MAX_IMAGE_EDGE', 800):
            smaller = model_payload(self.batch, student, self.root, 'test', 'grade')
        summary = request_summary(smaller)
        self.assertGreater(len(json.dumps(original).encode()), 1024 * 1024)
        self.assertLess(summary['bytes'], 1024 * 1024)
        old_parts = original['messages'][1]['content']
        new_parts = smaller['messages'][1]['content']
        self.assertEqual(len(old_parts), len(new_parts))
        self.assertEqual(old_parts[:-4], new_parts[:-4])  # All reference images unchanged.
        self.assertEqual(old_parts[-4], new_parts[-4])
        self.assertEqual(old_parts[-2], new_parts[-2])
        for part in (new_parts[-3], new_parts[-1]):
            raw = base64.b64decode(part['image_url']['url'].split(',')[1])
            with Image.open(io.BytesIO(raw)) as image:
                self.assertEqual(image.size, (600, 800))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before_hash)

    def test_small_requests_remain_identical_and_review_draft_is_retained(self):
        initial = {'test': 'review draft'}
        student = self.batch['students'][0]
        original = model_payload(self.batch, student, self.root, 'test', 'review', initial)
        with patch('yxin_grader.MAX_REQUEST_BYTES', 100 * 1024 * 1024):
            unchanged = model_payload(self.batch, student, self.root, 'test', 'review', initial)
        self.assertEqual(original, unchanged)
        self.assertIn('review draft', original['messages'][1]['content'][-1]['text'])

    def test_many_small_pngs_are_compressed_when_total_request_exceeds_budget(self):
        path = self.root / 'scan.png'
        Image.effect_noise((400, 400), 20).convert('RGB').save(path)
        original_file = path.read_bytes()
        self.assertLess(len(original_file), 512 * 1024)
        student = self.batch['students'][0]
        student['pages'] = [{'id': f'p{n}', 'attachment_id': 'a1', 'page': n,
                             'path': 'scan.png'} for n in range(1, 21)]
        with patch('yxin_grader.MAX_REQUEST_BYTES', 100 * 1024 * 1024):
            original = model_payload(self.batch, student, self.root, 'test', 'grade')
        budget = len(json.dumps(original).encode('utf-8')) // 2
        with patch('yxin_grader.MAX_REQUEST_BYTES', budget):
            smaller = model_payload(self.batch, student, self.root, 'test', 'grade')
        self.assertLessEqual(request_summary(smaller)['bytes'], budget)
        before = original['messages'][1]['content']
        after = smaller['messages'][1]['content']
        self.assertEqual(len(before), len(after))
        self.assertEqual([p for p in before if p['type'] == 'text'],
                         [p for p in after if p['type'] == 'text'])
        self.assertEqual(request_summary(original)['images'][-1]['width'], 400)
        self.assertEqual(request_summary(smaller)['images'][-1]['width'], 400)
        self.assertEqual(path.read_bytes(), original_file)

    def test_request_still_over_budget_is_logged_and_never_sent(self):
        with patch('yxin_grader.MAX_REQUEST_BYTES', 1):
            with self.assertRaisesRegex(GradingError, '未发送'):
                grade_batch(self.batch, self.root, ('key', 'https://yxai.chat/v1', self.pairs),
                            transport=lambda *_: self.fail('over-budget payload sent'), catalog=self.names)
        ledger = read_json(next((self.root / 'local' / 'yxin-runs').glob('*/usage.json')))
        self.assertEqual(ledger['calls'][0]['state'], 'not_sent')
        self.assertGreater(ledger['calls'][0]['request']['bytes'], 1)
        self.assertEqual(ledger['failure']['stage'], 'grade')
        with patch('yxin_grader.MAX_REQUEST_BYTES', 1), patch('yxin_grader.request.build_opener') as build:
            with self.assertRaisesRegex(GradingError, '未发送'):
                call_yxin({'model': 'test'}, 'key', 'https://yxai.chat/v1')
        build.assert_not_called()

    def test_provider_network_fluctuation_retries_only_twice(self):
        body = json.dumps({'error': {'message': '检测到当前网络波动~请重试【错误码：10065】'}}).encode()
        def failure():
            return HTTPError('https://yxai.chat/v1/chat/completions', 403, 'Forbidden', {}, io.BytesIO(body))
        class Opener:
            calls = 0
            def open(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise failure()
                return io.BytesIO(b'{"id":"ok"}')
        opener = Opener()
        with patch('yxin_grader.request.build_opener', return_value=opener), patch('yxin_grader.time.sleep') as sleep:
            result = call_yxin({'model': 'test'}, 'key', 'https://yxai.chat/v1')
        self.assertEqual(opener.calls, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result['_yxin_retry_count'], 2)
        self.assertEqual(len(result['_yxin_failed_attempts']), 2)
        self.assertEqual(result['_yxin_failed_attempts'][0]['provider_code'], '10065')

    def test_review_failure_persists_each_attempt_and_context(self):
        data = {'reviewed_pages': ['p1'], 'submission_mismatch': False,
                'questions': [{'number': n, 'status': 'correct', 'reason': '', 'evidence': []}
                              for n in (1, 2)]}
        body = json.dumps({'error': {'message': '错误码：10065 secret-test-key',
                                    'request_id': 'body-trace'}}).encode()
        responses = [io.BytesIO(json.dumps({'id': 'grade-ok', 'choices': [
            {'finish_reason': 'stop', 'message': {'content': json.dumps(data)}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode())]
        responses.extend(HTTPError('https://yxai.chat/v1/chat/completions', 403, 'Forbidden',
                                   {'x-request-id': f'trace-{n}'}, io.BytesIO(body)) for n in range(3))
        with patch('yxin_grader.request.build_opener') as build, patch('yxin_grader.time.sleep'):
            build.return_value.open.side_effect = responses
            with self.assertRaisesRegex(GradingError, 'gpt-5.6-sol · 审查阶段失败'):
                grade_batch(self.batch, self.root, ('secret-test-key', 'https://yxai.chat/v1', self.pairs),
                            catalog=self.names)
        path = next((self.root / 'local' / 'yxin-runs').glob('*/usage.json'))
        ledger = read_json(path)
        self.assertEqual(ledger['state'], 'failed')
        self.assertEqual(ledger['returned_calls'], 1)
        self.assertEqual(ledger['failure']['stage'], 'review')
        self.assertEqual(ledger['failure']['student_id'], 's1')
        self.assertEqual(ledger['failure']['model'], 'gpt-5.6-sol')
        attempts = ledger['calls'][1]['error']['attempts']
        self.assertEqual([a['attempt'] for a in attempts], [1, 2, 3])
        self.assertEqual(attempts[2]['response_headers']['x-request-id'], 'trace-2')
        self.assertIn('body-trace', attempts[2]['response_body'])
        self.assertNotIn('secret-test-key', path.read_text(encoding='utf-8'))
        self.assertFalse(attempts[2]['body_truncated'])

    def test_connection_failure_records_details_without_retry(self):
        with patch('yxin_grader.request.build_opener') as build, patch('yxin_grader.time.sleep') as sleep:
            build.return_value.open.side_effect = URLError('connection refused')
            with self.assertRaisesRegex(GradingError, '批改阶段失败'):
                grade_batch(self.batch, self.root, ('key', 'https://yxai.chat/v1', self.pairs),
                            catalog=self.names)
        sleep.assert_not_called()
        ledger = read_json(next((self.root / 'local' / 'yxin-runs').glob('*/usage.json')))
        attempt = ledger['failure']['error']['attempts'][0]
        self.assertEqual(attempt['exception_type'], 'URLError')
        self.assertIn('connection refused', attempt['message'])
        self.assertEqual(ledger['calls'][0]['state'], 'sent_or_unknown')

    def test_non_json_http_body_is_bounded_and_marked(self):
        exception = HTTPError('https://yxai.chat/v1/chat/completions', 502, 'Bad Gateway',
                              {}, io.BytesIO(b'x' * 70000))
        with patch('yxin_grader.request.build_opener') as build:
            build.return_value.open.side_effect = exception
            with self.assertRaises(GradingError) as caught:
                call_yxin({'model': 'test'}, 'key', 'https://yxai.chat/v1')
        attempt = caught.exception.attempts[0]
        self.assertTrue(attempt['body_truncated'])
        self.assertEqual(len(attempt['response_body']), 65536)
        self.assertEqual(attempt['http_status'], 502)

    def test_other_http_403_is_not_retried(self):
        body = json.dumps({'error': {'message': 'access denied'}}).encode()
        exception = HTTPError('https://yxai.chat/v1/chat/completions', 403, 'Forbidden', {}, io.BytesIO(body))
        class Opener:
            calls = 0
            def open(self, *_args, **_kwargs):
                self.calls += 1
                raise exception
        opener = Opener()
        with patch('yxin_grader.request.build_opener', return_value=opener), patch('yxin_grader.time.sleep') as sleep:
            with self.assertRaisesRegex(GradingError, 'HTTP 403'):
                call_yxin({'model': 'test'}, 'key', 'https://yxai.chat/v1')
        self.assertEqual(opener.calls, 1)
        sleep.assert_not_called()

    def test_settings_uses_yxin_without_echoing_key(self):
        (self.root / '.env').write_text('YXIN_BASE_URL=https://yxai.chat/\nYXIN_API_KEY=fake\n',
                                        encoding='utf-8')
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'YXIN_BASE_URL': '', 'YXIN_API_KEY': ''}):
            with self.assertRaises(GradingError):
                load_settings(self.root)
        with patch.dict(os.environ, {}, clear=True):
            key, endpoint, pairs = load_settings(self.root)
        self.assertEqual((key, endpoint, pairs), ('fake', 'https://yxai.chat/v1', self.pairs))


if __name__ == '__main__':
    unittest.main()

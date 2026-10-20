import copy
import json
import sys
import tempfile
import unittest
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib import request, error
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from canvas_batch import fingerprint
from canvas_review import review_state, submit_review
from canvas_roster import CanvasClient
from qwen_grader import GradingError, read_json, save_json
from test_canvas_roster import FakeResponse, FakeOpener


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.submission = {'user_id': 1, 'assignment_id': 428317, 'attempt': 1,
                           'submitted_at': '2026-09-20T10:00:00Z', 'submission_type': 'online_upload',
                           'attachments': [{'id': 4, 'content-type': 'image/png', 'size': 200}],
                           'score': None, 'submission_comments': []}
        self.assignment = {'id': 428317, 'course_id': 94139, 'points_possible': 100, 'grading_type': 'points'}
        self.batch = {'mode': 'real', 'assignment_url': 'https://oc.sjtu.edu.cn/courses/94139/assignments/428317',
                      'review_revision': 'rev1', 'students': [{'id': '1', 'source_fingerprint': fingerprint(self.submission)}]}
        self.body = {'assignment_url': self.batch['assignment_url'], 'student_id': '1',
                     'review_revision': 'rev1', 'score': '98', 'comment': '已核对：计算有误。'}
        owner = self
        class Client:
            writes = 0
            fail_after_write = False
            fail_before_write = False
            def read(self, path, params=None):
                if path.endswith('/profile'): return {'id': 9}
                return copy.deepcopy(owner.submission if '/submissions/' in path else owner.assignment)
            def submit_review(self, course, assignment, student, score, comment, attempt):
                self.writes += 1
                if self.fail_before_write: raise GradingError('HTTP 403')
                owner.submission['score'] = score
                if comment: owner.submission['submission_comments'].append({'id': 50, 'author_id': 9, 'comment': comment})
                if self.fail_after_write: raise GradingError('连接中断')
                return copy.deepcopy(owner.submission)
        self.client = Client()
        self.body['snapshot'] = review_state(self.client, self.batch, self.body)['snapshot']

    def test_submit_readback_and_duplicate_click_only_reads(self):
        result = submit_review(self.client, self.batch, self.body, self.folder)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(result['comment_id'], '50')
        self.assertEqual(self.client.writes, 1)
        again = submit_review(self.client, self.batch, self.body, self.folder)
        self.assertTrue(again['reused'])
        self.assertEqual(self.client.writes, 1)
        self.assertEqual(read_json(next(self.folder.glob('*.json')))['state'], 'verified')

    def test_timeout_after_applied_write_is_recovered_by_readback(self):
        self.client.fail_after_write = True
        self.assertEqual(submit_review(self.client, self.batch, self.body, self.folder)['state'], 'submitted')
        self.assertEqual(self.client.writes, 1)

    def test_state_recovers_receipt_despite_unrelated_new_comment(self):
        submit_review(self.client, self.batch, self.body, self.folder)
        self.submission['submission_comments'].append({'id': 60, 'author_id': 1, 'comment': '收到'})
        state = review_state(self.client, self.batch, self.body, self.folder)
        self.assertEqual(state['receipt']['score'], 98)
        self.assertEqual(state['receipt']['comment'], self.body['comment'])
        self.submission['score'] = 90
        self.assertIsNone(review_state(self.client, self.batch, self.body, self.folder)['receipt'])

    def test_state_does_not_treat_remote_grade_without_receipt_as_our_submission(self):
        self.submission['score'] = 98
        self.assertIsNone(review_state(self.client, self.batch, self.body, self.folder)['receipt'])

    def test_unverified_write_blocks_repeated_or_changed_comment(self):
        self.client.fail_before_write = True
        with self.assertRaisesRegex(GradingError, '状态待核验'):
            submit_review(self.client, self.batch, self.body, self.folder)
        with self.assertRaisesRegex(GradingError, '避免重复'):
            submit_review(self.client, self.batch, self.body, self.folder)
        self.body['comment'] = '另一条评语'
        with self.assertRaisesRegex(GradingError, '上一条'):
            submit_review(self.client, self.batch, self.body, self.folder)
        self.assertEqual(self.client.writes, 1)

    def test_external_grade_or_comment_change_prevents_write(self):
        for field, value in [('score', 70), ('submission_comments', [{'id': 10, 'comment': '新评论'}])]:
            original = copy.deepcopy(self.submission)
            self.submission[field] = value
            with self.assertRaisesRegex(GradingError, '已变化'):
                submit_review(self.client, self.batch, self.body, self.folder)
            self.submission = original
        self.assertEqual(self.client.writes, 0)

    def test_new_attempt_or_wrong_student_or_revision_is_rejected(self):
        self.submission['attempt'] = 2
        with self.assertRaisesRegex(GradingError, '答卷版本'):
            submit_review(self.client, self.batch, self.body, self.folder)
        self.submission['attempt'] = 1
        for key, value in [('student_id', '2'), ('review_revision', 'old'), ('assignment_url', 'https://evil.test')]:
            with self.assertRaises(GradingError):
                submit_review(self.client, self.batch, dict(self.body, **{key: value}), self.folder)
        self.assertEqual(self.client.writes, 0)

    def test_invalid_scores_and_demo_never_write(self):
        for value in ['', None, True, 'NaN', 'inf', -1, 101]:
            with self.assertRaises(GradingError):
                submit_review(self.client, self.batch, dict(self.body, score=value), self.folder)
        self.batch['mode'] = 'demo'
        with self.assertRaises(GradingError):
            submit_review(self.client, self.batch, self.body, self.folder)
        self.assertEqual(self.client.writes, 0)

    def test_blank_comment_only_sets_score(self):
        self.body['comment'] = '  '
        result = submit_review(self.client, self.batch, self.body, self.folder)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(self.submission['submission_comments'], [])

    def test_grade_only_partial_success_is_not_reported_as_complete(self):
        def partial(*_): self.submission['score'] = 98
        self.client.submit_review = partial
        with self.assertRaisesRegex(GradingError, '状态待核验'):
            submit_review(self.client, self.batch, self.body, self.folder)

    def test_same_text_by_different_author_does_not_confirm_write(self):
        def wrong_author(*_):
            self.submission.update(score=98, submission_comments=[{'id': 50, 'author_id': 8, 'comment': self.body['comment']}])
        self.client.submit_review = wrong_author
        with self.assertRaisesRegex(GradingError, '状态待核验'):
            submit_review(self.client, self.batch, self.body, self.folder)

    def test_verified_result_changed_on_canvas_is_not_overwritten(self):
        submit_review(self.client, self.batch, self.body, self.folder)
        self.submission['score'] = 80
        with self.assertRaisesRegex(GradingError, '避免重复'):
            submit_review(self.client, self.batch, self.body, self.folder)
        self.assertEqual(self.client.writes, 1)

    def test_put_uses_grade_and_comment_on_individual_attempt(self):
        opener = FakeOpener([FakeResponse({})])
        CanvasClient('secret', opener).submit_review('94139', '428317', '1', 98, '中文评语', 2)
        req = opener.requests[0]
        fields = parse_qs(req.data.decode())
        self.assertEqual(req.method, 'PUT')
        self.assertEqual(fields['submission[posted_grade]'], ['98'])
        self.assertEqual(fields['comment[text_comment]'], ['中文评语'])
        self.assertEqual(fields['comment[attempt]'], ['2'])
        self.assertEqual(fields['comment[group_comment]'], ['false'])
        self.assertNotIn('posted_at', fields)

    def test_local_http_route_requires_same_origin_and_returns_verified_result(self):
        import serve
        class QuietHandler(serve.Handler):
            def log_message(self, *_): pass
        save_json(self.folder / 'local/batch-review.json', self.batch)
        server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        opener = request.build_opener(request.ProxyHandler({}))
        def post(path, body, origin):
            req = request.Request(base + path, data=json.dumps(body).encode(),
                                  headers={'Content-Type':'application/json', 'Origin':origin})
            with opener.open(req, timeout=5) as response: return json.load(response)
        try:
            with patch.object(serve, 'WEB', self.folder), patch.object(serve, 'load_canvas_token', return_value='test'), patch.object(serve, 'CanvasClient', return_value=self.client):
                with self.assertRaises(error.HTTPError) as caught:
                    post('/api/submit-review', self.body, 'https://evil.test')
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(self.client.writes, 0)
                state = post('/api/review-state', self.body, base)
                result = post('/api/submit-review', dict(self.body, snapshot=state['snapshot']), base)
                self.assertEqual(result['state'], 'submitted')
                self.assertEqual(self.client.writes, 1)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()

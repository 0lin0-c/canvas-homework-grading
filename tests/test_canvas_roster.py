import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib import parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from canvas_roster import CanvasClient, assignment_ids, read_roster, send_reminders
from qwen_grader import GradingError

URL = 'https://oc.sjtu.edu.cn/courses/94139/assignments/428317'


class FakeResponse:
    def __init__(self, value, headers=None):
        self.value = value
        self.headers = headers or {}
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, *_): return json.dumps(self.value).encode('utf-8')


class FakeOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
    def open(self, req, timeout):
        self.requests.append(req)
        return next(self.responses)


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.submissions = [
            {'assignment_id': 428317, 'user_id': 1, 'user': {'name': '已交同学'},
             'submitted_at': '2026-09-20T10:00:00Z', 'assignment_visible': True},
            {'assignment_id': 428317, 'user_id': 2, 'user': {'name': '未交同学'},
             'submitted_at': None, 'assignment_visible': True},
            {'assignment_id': 428317, 'user_id': 3, 'user': {'name': '免交同学'},
             'submitted_at': None, 'excused': True},
        ]
        self.assignment = {'id': 428317, 'course_id': 94139, 'name': '概率论习题一',
                           'due_at': '2026-09-30T16:00:00Z'}

    def test_roster_separates_submitted_missing_and_excused(self):
        class Client:
            def read(_, path, params=None):
                if params:
                    self.assertEqual(params['student_ids[]'], 'all')
                    self.assertEqual(params['assignment_ids[]'], '428317')
                    return self.submissions
                return self.assignment
        roster = read_roster(Client(), URL)
        self.assertEqual([s['id'] for s in roster['submitted']], ['1'])
        self.assertEqual([s['id'] for s in roster['unsubmitted']], ['2'])
        self.assertEqual(roster['excluded_count'], 1)
        self.assertEqual(roster['due_date'], '2026-10-01')

    def test_url_is_restricted_to_requested_canvas_assignment(self):
        self.assertEqual(assignment_ids(URL), ('94139', '428317'))
        for bad in ('http://oc.sjtu.edu.cn/courses/94139/assignments/428317',
                    'https://oc.sjtu.edu.cn.evil.test/courses/94139/assignments/428317',
                    URL + '?student=1'):
            with self.assertRaises(GradingError): assignment_ids(bad)

    def test_canvas_pagination_rejects_other_domain(self):
        first = FakeResponse([{'id': 1}], {'Link': '<https://evil.test/api/v1/next>; rel="next"'})
        opener = FakeOpener([first])
        with self.assertRaisesRegex(GradingError, '分页链接'):
            CanvasClient('secret', opener).read('/api/v1/courses/94139/students/submissions')
        self.assertEqual(len(opener.requests), 1)

    def test_send_private_reminder_and_recheck_recipient(self):
        roster = {'assignment_url': URL, 'course_id': '94139', 'title': '概率论习题一',
                  'unsubmitted': [{'id': '2', 'name': '未交同学'}]}
        class Client:
            def send_private_message(_, course, recipients, subject, body):
                self.assertEqual(course, '94139')
                self.assertEqual(recipients, ['2'])
                self.assertIn('概率论习题一', subject)
                self.assertIn('请尽快通过Canvas提交', body)
                self.assertNotIn('日前', body)
                return [{'id': 77, 'private': True, 'audience': [2]}]
        result = send_reminders(Client(), roster, ['2'], self.folder)
        self.assertEqual(result['sent_count'], 1)
        with self.assertRaisesRegex(GradingError, '已经发送'):
            send_reminders(Client(), roster, ['2'], self.folder)
        with self.assertRaisesRegex(GradingError, '当前仍未提交'):
            send_reminders(Client(), roster, ['1'], self.folder)

    def test_canvas_message_is_private_form_submission(self):
        opener = FakeOpener([FakeResponse([{'id': 77}])])
        CanvasClient('secret', opener).send_private_message('94139', ['2', '4'], '作业提醒', '请提交')
        req = opener.requests[0]
        fields = parse.parse_qs(req.data.decode('utf-8'))
        self.assertEqual(fields['recipients[]'], ['2', '4'])
        self.assertEqual(fields['group_conversation'], ['false'])
        self.assertEqual(fields['context_code'], ['course_94139'])


if __name__ == '__main__': unittest.main()

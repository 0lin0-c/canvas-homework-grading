import copy
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib import error, request

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import serve
from canvas_batch import prepare_batch
from qwen_grader import GradingError, read_json, save_json
from reference_index import (build_reference_index, parse_exercises, resolve_assignment_source,
                             verify_assignment_references)

URL = 'https://oc.sjtu.edu.cn/courses/94139/assignments/999999'


class ReferenceIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.assets = {}
        for name in ('q1', 'q2', 'a1', 'a2', 'other'):
            path = self.root / (name + '.png')
            Image.new('RGB', (20, 20), 'white').save(path)
            self.assets[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        def ref(question, answer):
            return {'question_images': [{'path': question + '.png', 'source': '原题'}],
                    'answer_images': [{'path': answer + '.png', 'source': '官方答案'}]}
        self.catalog = {'course_id': '94139', 'book_id': 'book-a', 'book_title': '教材',
                        'assets': self.assets, 'chapters': {
                            '1': {'title': '习题一', 'questions': {'1': ref('q1', 'a1'),
                                                              '2': ref('q2', 'a2')}},
                            '2': {'title': '习题二', 'questions': {'1': ref('other', 'a2')}}}}
        self.catalog_path = self.root / 'local/reference-library/catalog.json'
        save_json(self.catalog_path, self.catalog)
        self.assignment = {'id': 999999, 'course_id': 94139, 'name': '新作业',
                           'description': '<p>习题一：1、2</p>', 'due_at': None}
        self.submissions = [{'assignment_id': 999999, 'user_id': 1, 'user': {'name': '测试同学'},
                             'submitted_at': '2026-10-11T01:00:00Z', 'submission_type': 'online_upload',
                             'attempt': 1, 'late': False, 'seconds_late': 0,
                             'attachments': [{'id': 10, 'content-type': 'image/png',
                                              'size': (self.root / 'q1.png').stat().st_size}]}]
        owner = self
        class Client:
            def read(self, path, params=None):
                return copy.deepcopy(owner.submissions if params else owner.assignment)
        self.client = Client()

    def test_html_chinese_numbers_ranges_and_cross_page_references(self):
        self.assertEqual(parse_exercises('<p>习题一：21，23,&nbsp;25，26</p>'), ('1', [21, 23, 25, 26]))
        self.assertEqual(parse_exercises('<p>第十二章习题：第１、２、６—８题</p>'), ('12', [1, 2, 6, 7, 8]))
        self.assertEqual(parse_exercises('习题二：1至3、3'), ('2', [1, 2, 3]))
        self.assertEqual(parse_exercises('<p>课本习题一: 1,&nbsp; 2,&nbsp; 6, 7,&nbsp; 8, 10,&nbsp; 11,&nbsp; 12,&nbsp; 15</p>'),
                         ('1', [1, 2, 6, 7, 8, 10, 11, 12, 15]))
        ref = self.catalog['chapters']['1']['questions']['1']
        ref['question_images'].append({'path': 'q2.png', 'source': '跨页续题'})
        save_json(self.catalog_path, self.catalog)
        source = resolve_assignment_source(self.client, URL, self.root)
        self.assertEqual(len(source['reference_materials']['questions']['1']['question_images']), 2)
        self.assertEqual(source['question_numbers'], [1, 2])
        self.assertEqual(source['students'], [])

    def test_ambiguous_scope_is_not_silently_truncated(self):
        for description in ('1,2', '习题一：1(2)、3', '习题一：1-5，除3',
                            '习题一：1；习题二：2', '习题一：1-3，选做4',
                            '习题一：3-1', '习题一：0', '习题一：1.2', '习题一：', '习题123'):
            with self.subTest(description=description), self.assertRaises(GradingError):
                parse_exercises(description)

    def test_chapter_and_course_are_part_of_identity(self):
        self.assignment['description'] = '习题二：1'
        source = resolve_assignment_source(self.client, URL, self.root)
        self.assertEqual(source['reference_materials']['questions']['1']['question_images'][0]['path'], 'other.png')
        self.catalog['course_id'] = '7'
        save_json(self.catalog_path, self.catalog)
        with self.assertRaisesRegex(GradingError, '当前课程'):
            resolve_assignment_source(self.client, URL, self.root)

    def test_missing_answers_or_unknown_numbers_report_exact_scope(self):
        self.catalog['chapters']['1']['questions']['2']['answer_images'] = []
        save_json(self.catalog_path, self.catalog)
        self.assignment['description'] = '习题一：1、2、3'
        with self.assertRaisesRegex(GradingError, '第2题：缺少官方答案；第3题：原题与官方答案未收录'):
            resolve_assignment_source(self.client, URL, self.root)

    def test_changed_and_missing_assets_cannot_reuse_stale_index(self):
        build_reference_index(self.root)
        (self.root / 'a1.png').write_bytes(b'changed')
        with self.assertRaisesRegex(GradingError, '第1题：官方答案文件已变化'):
            resolve_assignment_source(self.client, URL, self.root)
        (self.root / 'a1.png').unlink()
        with self.assertRaisesRegex(GradingError, '第1题：官方答案文件缺失'):
            resolve_assignment_source(self.client, URL, self.root)
        self.assignment['description'] = '习题二：1'
        self.assertEqual(resolve_assignment_source(self.client, URL, self.root)['question_numbers'], [1])

    def test_preserve_unchanged_grading_signature_and_refresh_changed_spec(self):
        source = resolve_assignment_source(self.client, URL, self.root)
        source['reference_materials'] = {'version': 'legacy-v1',
                                        'questions': source['reference_materials']['questions']}
        source['students'] = [{'id': '1', 'source_fingerprint': 'unchanged'}]
        again = resolve_assignment_source(self.client, URL, self.root, source)
        self.assertEqual(again['reference_materials'], source['reference_materials'])
        self.assertEqual(again['students'], source['students'])
        verify_assignment_references(self.client, source, self.root)
        self.assignment['description'] = '习题一：2'
        with self.assertRaisesRegex(GradingError, '采集后发生变化'):
            verify_assignment_references(self.client, source, self.root)
        changed = resolve_assignment_source(self.client, URL, self.root, source)
        self.assertEqual(changed['question_numbers'], [2])
        self.assertEqual(list(changed['reference_materials']['questions']), ['2'])

    def test_rebuilding_unchanged_index_is_stable_and_new_verified_answers_are_picked_up(self):
        first = build_reference_index(self.root)
        path = self.root / 'local/reference-library/index.json'
        stamp = path.stat().st_mtime_ns
        self.assertEqual(build_reference_index(self.root), first)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.catalog['chapters']['1']['questions']['3'] = copy.deepcopy(self.catalog['chapters']['1']['questions']['1'])
        save_json(self.catalog_path, self.catalog)
        self.assignment['description'] = '习题一：3'
        source = resolve_assignment_source(self.client, URL, self.root)
        self.assertEqual(source['question_numbers'], [3])
        self.assertNotEqual(source['reference_match']['index_version'], first['version'])

    def test_button_endpoints_prepare_unconfigured_assignment_and_stop_before_model_on_missing_answer(self):
        payload = (self.root / 'q1.png').read_bytes()
        def prepare(*args, **kwargs):
            return prepare_batch(*args, **kwargs, downloader=lambda *_: payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        def post(endpoint):
            req = request.Request(base + endpoint, data=json.dumps({'assignment_url': URL}).encode(),
                                  headers={'Origin': base, 'Content-Type': 'application/json'})
            try:
                with request.build_opener(request.ProxyHandler({})).open(req, timeout=5) as response:
                    return response.status, json.load(response)
            except error.HTTPError as exc:
                return exc.code, json.load(exc)
        with patch.object(serve, 'WEB', self.root), patch.object(serve, 'CanvasClient', return_value=self.client), \
                patch.object(serve, 'load_canvas_token', return_value='test'), \
                patch.object(serve, 'prepare_batch', side_effect=prepare) as prepare_mock, \
                patch.object(serve, 'load_settings', return_value=('test', 'test', [])), \
                patch.object(serve, 'grade_batch', return_value={'completed': True}) as grade_mock:
            status, data = post('/api/prepare-batch')
            self.assertEqual(status, 200, data)
            self.assertEqual(data['reference_match']['question_numbers'], [1, 2])
            self.assertEqual(data['page_count'], 1)
            saved = self.root / 'local/assignments/94139-999999/batch-source.json'
            before = saved.read_bytes()
            self.assertEqual(read_json(saved)['reference_match']['chapter'], '1')
            self.assertEqual(post('/api/grade-yxin')[0], 200)
            grade_mock.assert_called_once()
            grade_mock.reset_mock()
            self.assignment['description'] = '习题一：99'
            status, data = post('/api/prepare-batch')
            self.assertEqual(status, 400)
            self.assertIn('第99题', data['error'])
            self.assertEqual(prepare_mock.call_count, 1)
            self.assertEqual(saved.read_bytes(), before)
            self.assertEqual(post('/api/grade-yxin')[0], 400)
            grade_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()

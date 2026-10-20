import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from canvas_batch import download_attachment, prepare_batch, verify_current_batch
from qwen_grader import GradingError, read_json

URL = 'https://oc.sjtu.edu.cn/courses/94139/assignments/428317'


class Client:
    def __init__(self, submissions):
        self.submissions = submissions

    def read(self, path, params=None):
        if params:
            return self.submissions
        return {'id': 428317, 'course_id': 94139, 'name': '本次作业', 'due_at': None}


class BatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        Image.new('RGB', (50, 50), 'white').save(self.root / 'ref.png')
        stream = io.BytesIO()
        Image.new('RGB', (50, 50), 'black').save(stream, format='PNG')
        self.payload = stream.getvalue()
        attachment = {'id': 10, 'url': 'https://oc.sjtu.edu.cn/files/10/download',
                      'content-type': 'image/png', 'size': len(self.payload), 'updated_at': 'now'}
        self.submissions = [
            {'assignment_id': 428317, 'user_id': 1, 'user': {'name': '已交'},
             'submitted_at': '2026-09-20T10:00:00Z', 'submission_type': 'online_upload',
             'attempt': 1, 'attachments': [attachment]},
            {'assignment_id': 428317, 'user_id': 2, 'user': {'name': '未交'}, 'submitted_at': None},
        ]
        self.client = Client(self.submissions)
        self.source = {'assignment_url': URL, 'question_numbers': [1],
                       'reference_materials': {'questions': {'1': {
                           'question_images': [{'path': 'ref.png'}],
                           'answer_images': [{'path': 'ref.png'}]}}}, 'students': []}

    def test_fetch_all_pages_and_reuse_unchanged_submission(self):
        downloads = []
        def fetch(client, item):
            downloads.append(item['id'])
            return self.payload
        batch, counts = prepare_batch(self.client, URL, self.root, self.source, downloader=fetch)
        self.assertEqual(counts, {'submitted_count': 1, 'attachment_count': 1,
                                  'page_count': 1, 'unsubmitted_count': 1})
        self.assertEqual(downloads, [10])
        self.assertEqual(len(batch['students'][0]['pages']), 1)
        self.assertEqual(read_json(self.root / 'local/batch-source.json'), batch)
        verify_current_batch(self.client, batch)
        again, _ = prepare_batch(self.client, URL, self.root, batch, downloader=fetch)
        self.assertEqual(downloads, [10])
        self.assertEqual(again['students'][0]['pages'], batch['students'][0]['pages'])

    def test_changed_submission_must_refresh_and_failure_preserves_input(self):
        batch, _ = prepare_batch(self.client, URL, self.root, self.source,
                                 downloader=lambda *_: self.payload)
        self.submissions[0]['attempt'] = 2
        with self.assertRaisesRegex(GradingError, '发生变化'):
            verify_current_batch(self.client, batch)
        def failed_download(*_):
            raise GradingError('下载失败')
        with self.assertRaisesRegex(GradingError, '下载失败'):
            prepare_batch(self.client, URL, self.root, batch, downloader=failed_download)
        self.assertEqual(read_json(self.root / 'local/batch-source.json'), batch)

    def test_untrusted_attachment_url_is_blocked_before_download(self):
        class TokenClient:
            token = 'secret'
        with self.assertRaisesRegex(GradingError, '地址不符合预期'):
            download_attachment(TokenClient(), {'id': 10, 'url': 'https://other.test/files/10/download',
                                                 'size': 1})

    def test_lateness_is_collected_and_changes_invalidate_batch(self):
        self.submissions[0].update(late=True, seconds_late=86400)
        batch, _ = prepare_batch(self.client, URL, self.root, self.source,
                                 downloader=lambda *_: self.payload)
        self.assertEqual(batch['students'][0]['seconds_late'], 86400)
        self.assertTrue(batch['students'][0]['late'])
        self.submissions[0]['seconds_late'] = 172800
        with self.assertRaisesRegex(GradingError, '发生变化'):
            verify_current_batch(self.client, batch)
        updated, _ = prepare_batch(self.client, URL, self.root, batch,
                                   downloader=lambda *_: self.payload)
        self.assertEqual(updated['students'][0]['seconds_late'], 172800)
        self.assertNotEqual(updated['students'][0]['source_fingerprint'],
                            batch['students'][0]['source_fingerprint'])

    def test_preparation_can_save_to_assignment_specific_path(self):
        path = self.root / 'local' / 'assignments' / '94139-428317' / 'batch-source.json'
        batch, _ = prepare_batch(self.client, URL, self.root, self.source,
                                 downloader=lambda *_: self.payload, output_path=path)
        self.assertEqual(read_json(path), batch)
        self.assertFalse((self.root / 'local' / 'batch-source.json').exists())


if __name__ == '__main__':
    unittest.main()

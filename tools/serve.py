"""Loopback-only grading workbench and individual human-confirmed Canvas submission."""
import argparse
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from canvas_batch import prepare_batch, verify_current_batch
from assignment_store import assignment_catalog, batch_path, load_batch
from reference_index import resolve_assignment_source, verify_assignment_references
from canvas_roster import CanvasClient, load_canvas_token, read_roster, send_reminders
from canvas_review import review_state, submit_review
from qwen_grader import GradingError
from yxin_grader import grade_batch, load_settings

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'prototypes'
LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def send_head(self):
        if self.path.split('?', 1)[0].endswith(('.html', '.js')):
            # Always reload workbench code; cached versions may have obsolete submit behavior.
            for name in ('If-Modified-Since', 'If-None-Match'):
                if name in self.headers:
                    del self.headers[name]
        return super().send_head()

    def end_headers(self):
        if self.path.split('?', 1)[0].endswith(('.html', '.js')):
            self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def reply(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        host = self.headers.get('Host', '')
        if host not in allowed or self.headers.get('Origin') != 'http://' + host:
            return self.reply(403, {'error': '仅允许本机同源页面调用。'})
        if self.path not in {'/api/prepare-batch', '/api/grade-yxin', '/api/assignment-roster', '/api/remind-unsubmitted',
                             '/api/review-state', '/api/submit-review', '/api/assignments', '/api/assignment-review'}:
            return self.reply(404, {'error': '接口不存在。'})
        if not LOCK.acquire(blocking=False):
            return self.reply(409, {'error': '已有批改正在进行，请勿重复运行。'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= (65536 if self.path == '/api/submit-review' else 4096) or self.headers.get('Content-Type') != 'application/json':
                return self.reply(400, {'error': '请求格式错误。'})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise GradingError('请求格式错误。')
            url = body.get('assignment_url', '').rstrip('/')
            if self.path == '/api/assignments':
                result = assignment_catalog(CanvasClient(load_canvas_token(ROOT)), WEB)
            elif self.path == '/api/assignment-review':
                result = {'batch': load_batch(WEB, url, 'review')}
            elif self.path in {'/api/review-state', '/api/submit-review'}:
                client = CanvasClient(load_canvas_token(ROOT))
                batch = load_batch(WEB, url, 'review')
                if batch is None:
                    raise GradingError('所选作业尚无批改结果。')
                result = (review_state(client, batch, body, WEB / 'local' / 'review-submissions') if self.path == '/api/review-state' else
                          submit_review(client, batch, body, WEB / 'local' / 'review-submissions'))
            elif self.path == '/api/prepare-batch':
                source = load_batch(WEB, url, 'source')
                client = CanvasClient(load_canvas_token(ROOT))
                source = resolve_assignment_source(client, url, WEB, source)
                _, result = prepare_batch(client,
                                          url, WEB, source, output_path=batch_path(WEB, url, 'source'))
            elif self.path == '/api/grade-yxin':
                batch = load_batch(WEB, url, 'source')
                if batch is None:
                    raise GradingError('这份作业尚未配置对应的原题和官方答案。')
                if body.get('assignment_url', '').rstrip('/') != batch['assignment_url']:
                    raise GradingError('该链接的完整答卷尚未采集，请先完成 Canvas 附件读取。')
                client = CanvasClient(load_canvas_token(ROOT))
                verify_assignment_references(client, batch, WEB)
                verify_current_batch(client, batch)
                result = grade_batch(batch, WEB, load_settings(ROOT), output_path=batch_path(WEB, url, 'review'))
            else:
                client = CanvasClient(load_canvas_token(ROOT))
                roster = read_roster(client, body.get('assignment_url', ''))
                if self.path == '/api/assignment-roster':
                    result = roster
                else:
                    result = send_reminders(client, roster, body.get('recipient_ids'),
                                            WEB / 'local' / 'reminder-ledger')
            self.reply(200, result)
        except GradingError as exc:
            self.reply(400, {'error': str(exc)})
        except Exception:
            self.reply(500, {'error': '操作未完成，请检查本机运行记录；若已发起提交，请先核验 Canvas，勿重复发送。'})
        finally:
            LOCK.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, directory=str(WEB)))
    print(f'YXIN workbench: http://127.0.0.1:{args.port}/review-workbench.html', flush=True)
    server.serve_forever()

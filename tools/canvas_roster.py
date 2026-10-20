"""Read current Canvas submissions and send private Inbox reminders."""
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib import error, parse, request
from zoneinfo import ZoneInfo

from qwen_grader import GradingError, NoRedirect, save_json

BASE = 'https://oc.sjtu.edu.cn'
ASSIGNMENT_PATH = re.compile(r'^/courses/(\d+)/assignments/(\d+)/?$')


def assignment_ids(url):
    parsed = parse.urlparse(url)
    match = ASSIGNMENT_PATH.fullmatch(parsed.path)
    if (parsed.scheme != 'https' or parsed.netloc != 'oc.sjtu.edu.cn' or not match
            or parsed.params or parsed.query or parsed.fragment):
        raise GradingError('请输入交大 Canvas 的完整作业链接。')
    return match.group(1), match.group(2)


def load_canvas_token(root):
    values = {}
    path = Path(root) / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                name, value = line.split('=', 1)
                values[name.strip()] = value.strip().strip('"').strip("'")
    token = os.environ.get('CANVAS_API_TOKEN', values.get('CANVAS_API_TOKEN', ''))
    if not token or token.startswith('YOUR_'):
        raise GradingError('请在本机 .env 配置 CANVAS_API_TOKEN，才能读取实时未提交名单和发送 Canvas 收件箱提醒。')
    return token


class CanvasClient:
    def __init__(self, token, opener=None):
        self.token = token
        self.opener = opener or request.build_opener(NoRedirect())

    def read(self, path, params=None):
        url = BASE + path + (('?' + parse.urlencode(params, doseq=True)) if params else '')
        items = []
        while url:
            parsed = parse.urlparse(url)
            if parsed.scheme != 'https' or parsed.netloc != 'oc.sjtu.edu.cn' or not parsed.path.startswith('/api/v1/'):
                raise GradingError('Canvas 分页链接不在允许的 API 域名内。')
            req = request.Request(url, headers={'Authorization': 'Bearer ' + self.token})
            data, headers = self._open(req)
            if not isinstance(data, list):
                if items:
                    raise GradingError('Canvas 分页响应格式不一致。')
                return data
            items.extend(data)
            link = headers.get('Link', '')
            url = next((part.split(';', 1)[0].strip(' <>') for part in link.split(',')
                        if re.search(r'rel="?next"?', part)), None)
        return items

    def send_private_message(self, course_id, recipients, subject, body):
        fields = [('recipients[]', recipient) for recipient in recipients]
        fields += [('subject', subject), ('body', body), ('context_code', 'course_' + course_id),
                   ('group_conversation', 'false'), ('mode', 'sync')]
        req = request.Request(BASE + '/api/v1/conversations',
                              data=parse.urlencode(fields).encode('utf-8'),
                              headers={'Authorization': 'Bearer ' + self.token,
                                       'Content-Type': 'application/x-www-form-urlencoded'})
        return self._open(req)[0]

    def submit_review(self, course_id, assignment_id, student_id, score, comment, attempt):
        fields = [('submission[posted_grade]', str(score))]
        if comment:
            fields += [('comment[text_comment]', comment), ('comment[attempt]', str(attempt)),
                       ('comment[group_comment]', 'false')]
        req = request.Request(BASE + f'/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/{student_id}',
                              method='PUT', data=parse.urlencode(fields).encode('utf-8'),
                              headers={'Authorization': 'Bearer ' + self.token,
                                       'Content-Type': 'application/x-www-form-urlencoded'})
        return self._open(req)[0]

    def _open(self, req):
        try:
            with self.opener.open(req, timeout=45) as response:
                return json.load(response), response.headers
        except error.HTTPError as exc:
            if exc.code == 401:
                raise GradingError('CANVAS_API_TOKEN 无效或已失效，请在项目 .env 中更换交大 Canvas 访问令牌。') from None
            raise GradingError(f'Canvas API 返回 HTTP {exc.code}；请检查令牌权限或稍后重试。') from None
        except (error.URLError, TimeoutError, ValueError):
            raise GradingError('Canvas API 连接或响应失败。发送请求若已发出，状态可能未知，请勿直接重试。') from None


def read_roster(client, assignment_url):
    course_id, assignment_id = assignment_ids(assignment_url)
    assignment = client.read(f'/api/v1/courses/{course_id}/assignments/{assignment_id}')
    if str(assignment.get('id')) != assignment_id or str(assignment.get('course_id')) != course_id:
        raise GradingError('Canvas 返回的作业与输入链接不一致。')
    submissions = client.read(f'/api/v1/courses/{course_id}/students/submissions',
                              {'student_ids[]': 'all', 'assignment_ids[]': assignment_id,
                               'include[]': 'user', 'enrollment_state': 'active', 'per_page': 100})
    if not isinstance(submissions, list):
        raise GradingError('Canvas 未返回完整的学生提交清单。')
    seen, submitted, unsubmitted, excused = set(), [], [], 0
    for item in submissions:
        if str(item.get('assignment_id')) != assignment_id:
            raise GradingError('Canvas 提交清单包含其他作业。')
        student_id = str(item.get('user_id', ''))
        if not student_id or student_id in seen:
            raise GradingError('Canvas 学生清单缺少 ID 或存在重复。')
        seen.add(student_id)
        if item.get('excused') or item.get('assignment_visible') is False:
            excused += 1
            continue
        student = {'id': student_id, 'name': (item.get('user') or {}).get('name') or f'学生 {student_id}'}
        if item.get('submitted_at'):
            submitted.append(student)
        else:
            unsubmitted.append(student)
    due_at = assignment.get('due_at')
    due_date = ''
    if due_at:
        due_date = datetime.fromisoformat(due_at.replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    return {'assignment_url': assignment_url, 'course_id': course_id, 'assignment_id': assignment_id,
            'title': assignment.get('name') or f'作业 {assignment_id}', 'due_date': due_date,
            'submitted': submitted, 'unsubmitted': unsubmitted, 'excluded_count': excused,
            'total_count': len(seen)}


def reminder_text(roster):
    return (f'同学你好：\nCanvas 显示你尚未提交“{roster["title"]}”。'
            '请尽快通过Canvas提交：\n'
            f'{roster["assignment_url"]}\n如果你已经提交，请核对 Canvas 作业页面的状态。')


def send_reminders(client, roster, recipients, ledger_dir):
    missing = {item['id'] for item in roster['unsubmitted']}
    if (not isinstance(recipients, list) or not recipients or len(recipients) > 100
            or any(not isinstance(value, str) or not value.isdecimal() for value in recipients)
            or len(set(recipients)) != len(recipients)
            or not set(recipients) <= missing):
        raise GradingError('收件人必须是当前仍未提交且未免交的学生。')
    body = reminder_text(roster)
    subject = ('作业提交提醒：' + roster['title'])[:255]
    identity = json.dumps([roster['assignment_url'], sorted(recipients), body], ensure_ascii=False)
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    record = Path(ledger_dir) / (digest + '.json')
    if record.exists():
        raise GradingError('同一批提醒已经发送或发送状态未明；为避免重复，请先核对 Canvas 收件箱。')
    save_json(record, {'state': 'sent_or_unknown', 'assignment_url': roster['assignment_url'],
                       'recipient_ids': recipients})
    result = client.send_private_message(roster['course_id'], recipients, subject, body)
    received = {str(item['audience'][0]) for item in result
                if isinstance(item, dict) and item.get('private') is True and len(item.get('audience', [])) == 1}
    if not isinstance(result, list) or received != set(recipients):
        raise GradingError('Canvas 已响应，但收件人回执不完整；请到 Canvas 收件箱核对，勿直接重试。')
    save_json(record, {'state': 'sent', 'assignment_url': roster['assignment_url'],
                       'recipient_ids': recipients,
                       'conversation_ids': [item.get('id') for item in result]})
    return {'sent_count': len(recipients), 'recipient_ids': recipients}
